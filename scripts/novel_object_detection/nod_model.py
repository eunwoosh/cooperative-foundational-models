from __future__ import annotations

import os
from pathlib import Path

import numpy as np
import transforms as T2
import torch
import cv2
import detectron2.data.transforms as T
from sklearn.preprocessing import MinMaxScaler
from PIL import Image
from detectron2.data import MetadataCatalog
from detectron2.structures import Instances, Boxes
from torch import nn
from torchvision.ops import box_convert
from segment_anything.utils.amg import batched_mask_to_box

from utils import BBoxVisualizer, get_clip_preds, read_image


def prepare_image_for_GDINO(input, device = "cuda"):
    """
    inputs: dict, with keys "file_name", "height", "width", "image", "image_id"
    outputs: transformed images
    """
    transform = T2.Compose(
        [
            T2.RandomResize([800], max_size=1333),
            T2.ToTensor(),
            T2.Normalize([0.485, 0.456, 0.406], [0.229, 0.224, 0.225]),
        ]
    )
    
    image_src = Image.open(input["file_name"]).convert("RGB")
    image = np.asarray(image_src)
    image_transformed, _ = transform(image_src, None)
    image_transformed = image_transformed.to(device)
    return image_transformed[None], image

    
class NOD:
    def __init__(self, param_dict: dict, gdino_model):
        self.gdino_model = gdino_model
        self.positive_map_list = param_dict["positive_map_list"]
        self.length = param_dict["class_len_per_prompt"]
        self.selvisualize = param_dict["visualize"]
        self.out_dir = param_dict["out_dir"]
        self.lvis_data_split = param_dict["lvis_data_split"]
        self.rcnn_model = param_dict["rcnn_model"]
        #CLIP
        self.clip_model = param_dict["clip_model"]
        self.preprocess = param_dict["preprocess"]
        self.text_features = param_dict["text_features"]
        self.device = param_dict["device"]
        self.coco_to_lvis = param_dict["coco_to_lvis"]

        #SAM
        self.sam = param_dict["sam"]
        self.resize_transform = param_dict["resize_transform"]

        self.rcnn_model.eval()

        if not isinstance(self.rcnn_model.roi_heads.box_predictor, nn.ModuleList):  # baseline, non-centernet
            self.rcnn_model.proposal_generator.nms_thresh = 0.9
        else:
            self.rcnn_model.proposal_generator.nms_thresh_train = 0.9
            self.rcnn_model.proposal_generator.nms_thresh_test = 0.9

        if isinstance(self.rcnn_model.roi_heads.box_predictor, nn.ModuleList):
            box_predictors = self.rcnn_model.roi_heads.box_predictor
        else:
            box_predictors = [self.rcnn_model.roi_heads.box_predictor]

        for box_predictor in box_predictors:
            box_predictor.allow_novel_classes_during_inference = True
            box_predictor.allow_novel_classes_during_training = True
            box_predictor.test_topk_per_image = 300
            box_predictor.test_nms_thresh = 0.5
            box_predictor.test_score_thresh = 0.0001

    @torch.no_grad()
    def infer(
        self,
        image_path: str,
        text_prompt_list: list[str] = ["dog ."],
        visualize: bool = True,
        out_dir: Path | None = None,
        confidence_threshold: float = 0.5,
    ):
        inputs = self.prepare_inputs(image_path)

        outputs = self.rcnn_model(inputs)
        rcnn_boxes = outputs[0]["instances"].pred_boxes.tensor.to("cpu") # format: (x1, y1, x2, y2)
        rcnn_scores = outputs[0]["instances"].scores.to("cpu")
        rcnn_classes = outputs[0]["instances"].pred_classes.to("cpu")

        bg_boxes_idxs = rcnn_classes == 80
        bg_boxes = rcnn_boxes[bg_boxes_idxs].to("cpu")

        known_boxes = rcnn_boxes[~bg_boxes_idxs].to("cpu")
        known_scores = rcnn_scores[~bg_boxes_idxs].to("cpu")
        known_classes = rcnn_classes[~bg_boxes_idxs].to("cpu")

        known_classes = torch.tensor([self.coco_to_lvis[coco_class.item()] for coco_class in known_classes])

        img = inputs[0]['image']
        new_height = img.shape[1]
        new_width = img.shape[2]
        object_crop_1x_list = []
        selected_idx = []
        for bbox_idx, bbox in enumerate(bg_boxes):
            x1, y1, x2, y2 = bbox

            x1 = int(x1 * new_width / inputs[0]['width'])
            x2 = int(x2 * new_width / inputs[0]['width'])

            y1 = int(y1 * new_height / inputs[0]['height'])
            y2 = int(y2 * new_height / inputs[0]['height'])

            cropped_image = img[:, y1:y2, x1:x2]
            cropped_img_arr = cropped_image.permute(1, 2, 0).numpy()

            if cropped_img_arr.shape[0] > 0 and cropped_img_arr.shape[1] > 0:
                cropped_img_arr = cv2.cvtColor(cropped_img_arr, cv2.COLOR_BGR2RGB)
                img_pil = Image.fromarray(cropped_img_arr)
                image = self.preprocess(img_pil).unsqueeze(0).to(self.device)

                object_crop_1x_list.append(image)
                selected_idx.append(bbox_idx)
    
        selected_idx = torch.tensor(selected_idx)
        cropped_img_arr = torch.cat(object_crop_1x_list, dim = 0)
        scores_clip, indices_clip = get_clip_preds(cropped_img_arr, self.clip_model, self.text_features)

        bg_boxes = bg_boxes[selected_idx]
        bg_scores = scores_clip.squeeze(1).to("cpu")
        bg_classes = indices_clip.squeeze(1).to("cpu")

        combined_rcnn_boxes = torch.cat([known_boxes, bg_boxes], dim = 0)
        combined_rcnn_scores = torch.cat([known_scores, bg_scores], dim = 0)
        combined_rcnn_classes = torch.cat([known_classes, bg_classes], dim = 0)

        image, image_src = prepare_image_for_GDINO(inputs[0])
        image = image.repeat(len(text_prompt_list), 1, 1, 1)
        with torch.no_grad():
            output = self.gdino_model(image, captions = text_prompt_list)

        out_logits = output["pred_logits"]  # prediction_logits.shape = (batch, nq, 256)
        out_bbox = output["pred_boxes"] # prediction_boxes.shape = (batch, nq, 4)
        prob_to_token = out_logits.sigmoid() # prob_to_token.shape = (batch, nq, 256)

        prob_to_label_list = []
        for i in range(prob_to_token.shape[0]):
            # (nq, 256) @ (num_categories, 256).T -> (nq, num_categories)
            curr_prob_to_label = prob_to_token[i] @ self.positive_map_list[i].to(prob_to_token.device).T
            prob_to_label_list.append(curr_prob_to_label.to("cpu"))

        prob_to_label = torch.cat(prob_to_label_list, dim = 1) # shape: (nq, 1203)
        topk_values, topk_idxs = torch.topk(
            prob_to_label.view(-1), 300, 0
        )
        #topk_idxs contains the index of the flattened tensor. We need to convert it to the index in the original tensor
        scores = topk_values # Shape: (300,)
        topk_boxes = topk_idxs // prob_to_label.shape[1] # to determine the index in 'num_query' dimension. Shape: (300,)
        labels = topk_idxs % prob_to_label.shape[1] # to determine the index in 'num_category' dimension. Shape: (300,)
        topk_boxes_batch_idx = labels // self.length # to determine the index in 'batch_size' dimension. Shape: (300,)
        combined_box_index = torch.stack((topk_boxes_batch_idx, topk_boxes), dim=1)
        boxes = out_bbox[combined_box_index[:, 0], combined_box_index[:, 1]].to("cpu") # Shape: (300, 4)
        h, w = inputs[0]['height'], inputs[0]['width']
        boxes = boxes * torch.Tensor([w, h, w, h])
        boxes = box_convert(boxes = boxes, in_fmt = "cxcywh", out_fmt = "xyxy")

        boxes = torch.cat([combined_rcnn_boxes, boxes], dim = 0)
        scores = torch.cat([combined_rcnn_scores, scores], dim = 0)
        labels = torch.cat([combined_rcnn_classes, labels], dim = 0)

        labels = labels.to(torch.int64)

        # Standardize the combined scores of RCNN and GDINO 
        scaler = MinMaxScaler()
        scores = scaler.fit_transform(scores.reshape(-1, 1)).reshape(-1)
        scores = torch.tensor(scores, dtype = combined_rcnn_scores.dtype)

        #SAM
        boxes = boxes.to(self.sam.device)
        curr_image = cv2.imread(inputs[0]['file_name'])
        curr_image = cv2.cvtColor(curr_image, cv2.COLOR_BGR2RGB)

        img_shape = curr_image.shape[:2] # (h, w)
        curr_image = self.resize_transform.apply_image(curr_image)
        curr_image = torch.as_tensor(curr_image, device = self.sam.device).permute(2, 0, 1).contiguous()

        sam_box_prompts = self.resize_transform.apply_boxes_torch(boxes, img_shape)

        batched_input = [
            {
                "image": curr_image,
                "boxes": sam_box_prompts,
                "original_size": img_shape,
            }
        ]

        batched_output = self.sam(batched_input, multimask_output = False)
        sam_masks = batched_output[0]['masks']
        sam_refined_boxes = batched_mask_to_box(sam_masks.clone().detach()).squeeze(1)
        sam_scores = batched_output[0]['iou_predictions']
        sam_scores = sam_scores.squeeze(1).to("cpu")

        # Standardize the SAM scores
        scaler_sam = MinMaxScaler()
        sam_scores = scaler_sam.fit_transform(sam_scores.reshape(-1, 1)).reshape(-1)
        sam_scores = torch.tensor(sam_scores, dtype = scores.dtype)
        
        scores = scores * sam_scores
        topk_scores, topk_idxs = torch.topk(scores, 300)

        boxes = sam_refined_boxes[topk_idxs]
        labels = labels[topk_idxs]
        scores = topk_scores

        if visualize:
            result = Instances((h, w))
            score_mask = scores >= confidence_threshold
            boxes_vis = boxes[score_mask]
            scores_vis = scores[score_mask]
            labels_vis = labels[score_mask]

            result.pred_boxes = Boxes(boxes_vis)
            result.scores = scores_vis
            result.pred_classes = labels_vis

            meta_data = MetadataCatalog.get(self.lvis_data_split)
            Path(f"{out_dir}/output_images").mkdir(parents=True, exist_ok=True)

            im = cv2.imread(inputs[0]['file_name'])
            im = cv2.cvtColor(im, cv2.COLOR_BGR2RGB)

            Path(f"{out_dir}/raw_images").mkdir(parents=True, exist_ok=True)
            cv2.imwrite(f"{out_dir}/raw_images/{inputs[0]['file_name'].split('/')[-1]}", im[:, :, ::-1])

            v = BBoxVisualizer(im, meta_data, scale = 1.2)
            out = v.draw_instance_predictions(result)
            f_name = inputs[0]['file_name'].split('/')[-1]
            cv2.imwrite(f"{out_dir}/output_images/{f_name}", out.get_image()[:, :, ::-1])

        result = Instances((h, w))
        result.pred_boxes = Boxes(boxes)
        result.scores = scores
        result.pred_classes = labels

        final_outputs = []
        curr_output = {}
        curr_output['instances'] = result
        final_outputs.append(curr_output)

        return final_outputs

    def prepare_inputs(self, image_path: str, image_format: str = "BGR"):
        data_dict = {}
        inputs = []

        img = read_image(image_path, format = image_format)
        orig_height = img.shape[0]
        orig_width = img.shape[1]
        data_dict["file_name"] = os.path.abspath(image_path)
        data_dict["height"] = orig_height
        data_dict["width"] = orig_width
        data_dict["not_exhaustive_category_ids"] = []
        data_dict["neg_category_ids"] = []
        data_dict["image_id"] = 0

        augmentations = T.AugmentationList([T.ResizeShortestEdge(short_edge_length = 800, max_size = 1333)])

        aug_input = T.AugInput(img, sem_seg=None)
        transforms = augmentations(aug_input)

        image = aug_input.image
        data_dict["image"] = torch.as_tensor(np.ascontiguousarray(image.transpose(2, 0, 1)))

        inputs.append(data_dict)

        return inputs

    def infer_multiple_images(
        self,
        img_dir: Path,
        text_prompt_list: list[str] = ["dog ."],
        visualize: bool = True,
        out_dir: Path | None = None,
        confidence_threshold: float = 0.5,
    ):
        for img_file in img_dir.iterdir():
            self.infer(str(img_file), text_prompt_list, visualize, out_dir, confidence_threshold)

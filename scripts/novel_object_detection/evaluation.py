import torch
import logging
import datetime
import copy
import time
import json
import numpy as np
import os
import itertools
from tqdm import tqdm

from detectron2.utils.logger import log_every_n_seconds, create_small_table
from detectron2.evaluation import DatasetEvaluator, LVISEvaluator
from detectron2.evaluation.coco_evaluation import instances_to_coco_json
from detectron2.utils.file_io import PathManager
from lvis import LVISEval
from collections import OrderedDict

from ground_dino_utils import inference_gdino

@torch.no_grad()
def inference(data_loader, evaluator_discovery, model, text_prompt_list, param_dict):
    evaluator_discovery.reset()
    _run_generic_evaluation_loop(data_loader, evaluator_discovery, model, text_prompt_list, param_dict)
    
    results = evaluator_discovery.evaluate()
    if results is None:
        results = {}

    return results

def _run_generic_evaluation_loop(data_loader, evaluator, model, text_prompt_list, param_dict):
    logger = logging.getLogger(__name__)
    logger.info("###\n### Start custom inference on {} batches\n###".format(len(data_loader)))

    total = len(data_loader)  # inference data loader must have a fixed length
    num_warmup = min(5, total - 1)

    start_time = time.perf_counter()
    for idx, inputs in enumerate(tqdm(data_loader)):
        if idx == num_warmup:
            start_time = time.perf_counter()

        outputs = inference_gdino(model, inputs, text_prompt_list, param_dict)
        if torch.cuda.is_available():
            torch.cuda.synchronize()

        evaluator.process(inputs, outputs)

        iters_after_start = idx + 1 - num_warmup * int(idx >= num_warmup)
        total_seconds_per_iter = (time.perf_counter() - start_time) / iters_after_start
        if idx >= num_warmup * 2:
            eta = datetime.timedelta(seconds=int(total_seconds_per_iter * (total - idx - 1)))
            log_every_n_seconds(
                logging.INFO,
                (
                    f"Inference done {idx + 1}/{total}. "
                    f"Total: {total_seconds_per_iter:.4f} s/iter. "
                    f"ETA={eta}"
                ),
                n=5,
            )
        
        if idx != 0 and idx % 2500 == 0:
            torch.save(evaluator.evaluator._predictions, param_dict["out_dir"] + "/predictions_" + str(idx) + ".pt")

    # Measure the time only for this worker (before the synchronization barrier)
    total_time = time.perf_counter() - start_time
    total_time_str = str(datetime.timedelta(seconds=total_time))
    # NOTE this format is parsed by grep
    logger.info("Total inference time: {} ({:.6f} s / iter per device)".format(
        total_time_str, total_time / (total - num_warmup)
    ))



class NpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, np.integer):
            return int(obj)
        if isinstance(obj, np.floating):
            return float(obj)
        if isinstance(obj, np.ndarray):
            return obj.tolist()
        return super(NpEncoder, self).default(obj)


class CustomEvaluator(DatasetEvaluator):
    """
    Wrapper around existing D2 Evaluators that supports category re-mapping.
    Modifies the `process` method only.

    Note: currently the support has been checked only COCOEvaluator and LVISEvaluator, which both similarly process
    cache the inputs/outputs inside the `process()`. Other evaluators may be supported as is, but it is not guaranteed.
    """

    def __init__(self, evaluator):
        self.evaluator = evaluator

        self._debug_dumped = 0

    def reset(self):
        return self.evaluator.reset()

    def process(self, inputs, outputs):
        for input, output in zip(inputs, outputs):
            prediction = {"image_id": input["image_id"]}

            instances = output["instances"].to("cpu")
            prediction["instances"] = instances_to_coco_json(instances, input["image_id"])

            self.evaluator._predictions.append(prediction)

    def evaluate(self):
        return self.evaluator.evaluate()


class LVISEvaluatorCustom(LVISEvaluator):
    """
    Modifies the default LVISEvaluator by supporting printing evaluation results for a subset of classes only.
    """

    def __init__(
            self,
            dataset_name,
            tasks=None,
            distributed=True,
            output_dir=None,
            *,
            max_dets_per_image=None,
            known_class_ids=None
    ):
        super().__init__(dataset_name, tasks, distributed, output_dir, max_dets_per_image=max_dets_per_image)
        self.known_class_ids = known_class_ids

    def _eval_predictions(self, predictions):
        """
        Same as `LVISEvaluator`, code had to be re-copied to fix a reference to a new `_evaluate_predictions_on_lvis()`
        that is re-defined below.
        """

        self._logger.info("[Evaluator new] Preparing results in the LVIS format ...")
        lvis_results = list(itertools.chain(*[x["instances"] for x in predictions]))
        tasks = self._tasks or self._tasks_from_predictions(lvis_results)

        # LVIS evaluator can be used to evaluate results for COCO dataset categories.
        # In this case `_metadata` variable will have a field with COCO-specific category mapping.
        if hasattr(self._metadata, "thing_dataset_id_to_contiguous_id"):
            reverse_id_mapping = {
                v: k for k, v in self._metadata.thing_dataset_id_to_contiguous_id.items()
            }
            for result in lvis_results:
                result["category_id"] = reverse_id_mapping[result["category_id"]]
        else:
            # unmap the category ids for LVIS (from 0-indexed to 1-indexed)
            for result in lvis_results:
                result["category_id"] += 1

        if self._output_dir:
            file_path = os.path.join(self._output_dir, "lvis_instances_results.json")
            self._logger.info("[Evaluator new] Saving results to {}".format(file_path))
            with PathManager.open(file_path, "w") as f:
                f.write(json.dumps(lvis_results, cls=NpEncoder))
                f.flush()

        if not self._do_evaluation:
            self._logger.info("Annotations are not available for evaluation.")
            return

        self._logger.info("[Evaluator new] Evaluating predictions ...")
        for task in sorted(tasks):
            res = _evaluate_predictions_on_lvis(
                self._logger,
                self._lvis_api,
                lvis_results,
                task,
                max_dets_per_image=self._max_dets_per_image,
                class_names=self._metadata.get("thing_classes"),
                known_class_ids=self.known_class_ids,
            )
            self._results[task] = res


def _evaluate_predictions_on_lvis(
        logger, lvis_gt, lvis_results, iou_type, max_dets_per_image=None, class_names=None, known_class_ids=None
):
    """
    Same as the original implementation, except that extra evaluation on only known or only novel classes is performed
    if `known_class_ids` is provided. For that replaces object of `LVISEval` with `LVISEvalCustom`.
    """

    metrics = {
        "bbox": ["AP", "AP50", "AP75", "APs", "APm", "APl", "APr", "APc", "APf"],
        "segm": ["AP", "AP50", "AP75", "APs", "APm", "APl", "APr", "APc", "APf"],
    }[iou_type]

    if len(lvis_results) == 0:  # TODO: check if needed
        logger.warn("No predictions from the model!")
        return {metric: float("nan") for metric in metrics}

    if iou_type == "segm":
        lvis_results = copy.deepcopy(lvis_results)
        # When evaluating mask AP, if the results contain bbox, LVIS API will
        # use the box area as the area of the instance, instead of the mask area.
        # This leads to a different definition of small/medium/large.
        # We remove the bbox field to let mask AP use mask area.
        for c in lvis_results:
            c.pop("bbox", None)

    if max_dets_per_image is None:
        max_dets_per_image = 300  # Default for LVIS dataset

    from lvis import LVISEval, LVISResults

    logger.info(f"[Evaluator new] Evaluating with max detections per image = {max_dets_per_image}")
    lvis_results = LVISResults(lvis_gt, lvis_results, max_dets=max_dets_per_image)
    if known_class_ids is not None:
        lvis_eval = LVISEvalCustom(lvis_gt, lvis_results, iou_type, known_class_ids)
    else:
        lvis_eval = LVISEval(lvis_gt, lvis_results, iou_type)
    lvis_eval.run()
    lvis_eval.print_results()

    # Pull the standard metrics from the LVIS results
    results = lvis_eval.get_results()
    results = {metric: float(results[metric] * 100) for metric in metrics}
    logger.info("[Evaluator new] Evaluation results for {}: \n".format(iou_type) + create_small_table(results))

    if known_class_ids is not None:  # Print results for known and novel classes separately
        for results, subtitle in [
            (lvis_eval.results_known, "known classes only"),
            (lvis_eval.results_novel, "novel classes only"),
        ]:
            results = {metric: float(results[metric] * 100) for metric in metrics}
            logger.info("Evaluation results for {} ({}): \n".format(iou_type, subtitle) + create_small_table(results))

    return results


class LVISEvalCustom(LVISEval):
    """
    Extends `LVISEval` with printing results for known and novel classes only when `known_class_ids` is provided.
    """

    def __init__(self, lvis_gt, lvis_dt, iou_type="segm", known_class_ids=None):
        super().__init__(lvis_gt, lvis_dt, iou_type)

        # Remap categories list following the mapping applied to train data, - that is list all categories in a
        # consecutive order and use their indices; see: `lvis-api/lvis/eval.py` line 109:
        # https://github.com/lvis-dataset/lvis-api/blob/35f09cd7c5f313a9bf27b329ca80effe2b0c8a93/lvis/eval.py#L109
        if known_class_ids is None:
            self.known_class_ids = None
        else:
            self.known_class_ids = [self.params.cat_ids.index(c) for c in known_class_ids]

    def _summarize(
            self, summary_type, iou_thr=None, area_rng="all", freq_group_idx=None, subset_class_ids=None
    ):
        """Extends the default version by supporting calculating the results only for the subset of classes."""

        if subset_class_ids is None:  # Use all classes
            subset_class_ids = list(range(len(self.params.cat_ids)))

        aidx = [
            idx
            for idx, _area_rng in enumerate(self.params.area_rng_lbl)
            if _area_rng == area_rng
        ]

        if summary_type == 'ap':
            s = self.eval["precision"]
            if iou_thr is not None:
                tidx = np.where(iou_thr == self.params.iou_thrs)[0]
                s = s[tidx]
            if freq_group_idx is not None:
                subset_class_ids = list(set(subset_class_ids).intersection(self.freq_groups[freq_group_idx]))
                s = s[:, :, subset_class_ids, aidx]
            else:
                s = s[:, :, subset_class_ids, aidx]
        else:
            s = self.eval["recall"]
            if iou_thr is not None:
                tidx = np.where(iou_thr == self.params.iou_thrs)[0]
                s = s[tidx]
            s = s[:, subset_class_ids, aidx]

        if len(s[s > -1]) == 0:
            mean_s = -1
        else:
            mean_s = np.mean(s[s > -1])
        return mean_s

    def summarize(self):
        """Extends the default version by supporting calculating the results only for the subset of classes."""

        if not self.eval:
            raise RuntimeError("Please run accumulate() first.")

        if self.known_class_ids is None:
            eval_groups = [(self.results, None)]
        else:
            cat_ids_mapped_list = list(range(len(self.params.cat_ids)))
            novel_class_ids = list(set(cat_ids_mapped_list).difference(self.known_class_ids))
            self.results_known = OrderedDict()
            self.results_novel = OrderedDict()
            eval_groups = [
                (self.results, None),
                (self.results_known, self.known_class_ids),
                (self.results_novel, novel_class_ids),
            ]

        max_dets = self.params.max_dets

        for container, subset_class_ids in eval_groups:
            container["AP"]   = self._summarize('ap', subset_class_ids=subset_class_ids)
            container["AP50"] = self._summarize('ap', iou_thr=0.50, subset_class_ids=subset_class_ids)
            container["AP75"] = self._summarize('ap', iou_thr=0.75, subset_class_ids=subset_class_ids)
            container["APs"]  = self._summarize('ap', area_rng="small", subset_class_ids=subset_class_ids)
            container["APm"]  = self._summarize('ap', area_rng="medium", subset_class_ids=subset_class_ids)
            container["APl"]  = self._summarize('ap', area_rng="large", subset_class_ids=subset_class_ids)
            container["APr"]  = self._summarize('ap', freq_group_idx=0, subset_class_ids=subset_class_ids)
            container["APc"]  = self._summarize('ap', freq_group_idx=1, subset_class_ids=subset_class_ids)
            container["APf"]  = self._summarize('ap', freq_group_idx=2, subset_class_ids=subset_class_ids)

            key = "AR@{}".format(max_dets)
            container[key] = self._summarize('ar', subset_class_ids=subset_class_ids)

            for area_rng in ["small", "medium", "large"]:
                key = "AR{}@{}".format(area_rng[0], max_dets)
                container[key] = self._summarize('ar', area_rng=area_rng, subset_class_ids=subset_class_ids)


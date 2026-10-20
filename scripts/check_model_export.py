"""Exercise the real YOLO postprocessor across every class ID in Core ML.

Run in the isolated exporter environment. --unsafe-default reproduces the FP16 bug.
"""
import argparse
import types

import coremltools as ct
import numpy as np
import torch
from ultralytics.nn.modules.head import Detect
from ultralytics.utils.export.coreml import _coreml_gather

from export_household_model import model_precision


class Postprocessor(torch.nn.Module):
    def __init__(self, class_count):
        super().__init__()
        self.head = Detect(nc=class_count, ch=(16,))
        self.head._gather = types.MethodType(_coreml_gather, self.head)
        self.head.export = True
        self.head.format = "coreml"

    def forward(self, predictions):
        return self.head.postprocess(predictions)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--unsafe-default", action="store_true")
    args = parser.parse_args()
    count, rows = 4585, 300
    shape = (1, rows, 4 + count + 32)
    traced = torch.jit.trace(Postprocessor(count).eval(), torch.zeros(shape))
    kwargs = {} if args.unsafe_default else {"compute_precision": model_precision(ct)}
    model = ct.convert(traced, inputs=[ct.TensorType(name="predictions", shape=shape)],
                       convert_to="mlprogram", **kwargs)
    name = model.get_spec().description.output[0].name
    checked = set()
    for start in range(0, count, rows):
        expected = (np.arange(rows) + start) % count
        predictions = np.zeros(shape, dtype=np.float32)
        predictions[0, :, 0] = np.arange(rows)
        predictions[0, np.arange(rows), 4 + expected] = 0.99
        output = model.predict({"predictions": predictions})[name][0]
        indices = output[:, 0].astype(np.int32)
        actual = output[:, 5]
        assert np.array_equal(actual, expected[indices]), (
            f"Class IDs changed: {list(zip(expected[indices][actual != expected[indices]][:8], actual[actual != expected[indices]][:8]))}"
        )
        checked.update(actual.astype(np.int32).tolist())
    assert len(checked) == count
    print(f"Core ML postprocessor preserves all {count} class IDs exactly.")


if __name__ == "__main__":
    main()

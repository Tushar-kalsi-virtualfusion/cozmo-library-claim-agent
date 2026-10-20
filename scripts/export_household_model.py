"""Export the model-owned prompt-free vocabulary for the native Core ML adapter.

Use an isolated Python 3.12 environment, not the service environment:
ultralytics==8.4.180 torch==2.7.0 torchvision==0.22.0 coremltools==9.0
numpy==1.26.4 scikit-learn==1.5.1 opencv-python==4.11.0.86
Use the official yoloe-26s-seg-pf.pt checkpoint. No text encoder or custom prompts are used.
"""
import argparse
import json
from pathlib import Path


def model_precision(ct):
    # Class IDs are integers until the final floating-point detection tensor.
    # FP16 cannot represent every ID above 2048. Keep casts/concatenations in
    # FP32 so the model's complete vocabulary survives without rounded IDs.
    return ct.transform.FP16ComputePrecision(
        op_selector=lambda op: op.op_type not in {"cast", "concat"}
    )


def main():
    from ultralytics import YOLOE
    from ultralytics.nn.modules.head import LRPCHead
    import coremltools as ct
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--weights",type=Path,required=True)
    parser.add_argument("--output",type=Path,required=True)
    args=parser.parse_args()
    model=YOLOE(str(args.weights))
    labels=[model.names[i] for i in range(len(model.names))]
    # The upstream static Core ML path bypasses the prompt-free proposal filter.
    # Preserve that learned objectness filter with a fixed-shape masked tensor,
    # avoiding dynamic indexing and any app-maintained category lists.
    original_forward=LRPCHead.forward
    def gated_forward(head,cls_feat,loc_feat,conf):
        box,scores,mask=original_forward(head,cls_feat,loc_feat,conf)
        if head.enabled and not conf:
            keep=head.pf(cls_feat)[:,0].flatten(1).sigmoid() > .001
            scores=scores.masked_fill(~keep[:,None,:],-1e4)
        return box,scores,mask
    LRPCHead.forward=gated_forward
    original_convert = ct.convert
    def precise_convert(source, **kwargs):
        if kwargs.get("convert_to") == "mlprogram" and "inputs" in kwargs:
            kwargs["compute_precision"] = model_precision(ct)
        return original_convert(source, **kwargs)
    ct.convert = precise_convert
    try:
        model.export(format="coreml",imgsz=640,quantize=8,nms=False,device="cpu")
    finally:
        ct.convert = original_convert
        LRPCHead.forward = original_forward
    exported=args.weights.with_suffix(".mlpackage")
    ml=ct.models.MLModel(str(exported),skip_model_load=True)
    output=next(o for o in ml.get_spec().description.output if list(o.type.multiArrayType.shape)==[1,300,38])
    ml.user_defined_metadata["claim_contract"]=json.dumps({
        "version":1,"id":"yoloe-26s-promptfree-int8-v1","labels":labels,
        "input_size":640,"output_name":output.name,"output_shape":[1,300,38],"confidence_threshold":.35})
    ml.short_description="Household proposals; server verifies identity; LiDAR measures geometry."
    ml.save(str(args.output))
    print(args.output)


if __name__=="__main__":
    main()

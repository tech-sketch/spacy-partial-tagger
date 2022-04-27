from typing import Any, Optional, Tuple, cast

import torch
from spacy.tokens import Doc
from spacy.util import List, registry
from thinc.api import ArgsKwargs, Model, torch2xp, xp2torch
from thinc.shims.pytorch_grad_scaler import PyTorchGradScaler
from thinc.types import Floats2d, Floats3d
from torch.nn import Module
from transformers import AutoModel, AutoTokenizer, BatchEncoding


class TransformersWrapper(Module):
    def __init__(self, transformer: Module) -> None:
        super(TransformersWrapper, self).__init__()

        self.transformer = transformer

    def forward(self, inputs: BatchEncoding) -> Tuple[torch.Tensor, torch.Tensor]:
        lengths = inputs.attention_mask.sum(dim=-1)
        outputs = self.transformer(**inputs).last_hidden_state
        return outputs, lengths


@registry.architectures.register("spacy-partial-tagger.Tok2VecTransformer.v1")
def build_tok2vec_transformer(
    model_name: str,
    chunk_size: int = 0,
    *,
    mixed_precision: bool = False,
    grad_scaler: Optional[PyTorchGradScaler] = None
) -> Model[List[Doc], List[Floats2d]]:
    return Model(
        "tok2vec_transformer",
        forward=forward,
        init=init,
        dims={"nI": None, "nO": None},
        attrs={
            "model_name": model_name,
            "chunk_size": chunk_size,
            "tokenizer": AutoTokenizer.from_pretrained(model_name),
            "mixed_precision": mixed_precision,
            "grad_scaler": grad_scaler,
        },
    )


def init(model: Model, X: Any = None, Y: Any = None) -> None:
    if model.layers:
        return

    PyTorchWrapper = registry.get("layers", "PyTorchWrapper.v2")

    mixed_precision = model.attrs["mixed_precision"]
    grad_scaler = model.attrs["grad_scaler"]

    pytorch_model = AutoModel.from_pretrained(
        model.attrs["model_name"], chunk_size_feed_forward=model.attrs["chunk_size"]
    )

    transformer = PyTorchWrapper(
        TransformersWrapper(pytorch_model),
        mixed_precision=mixed_precision,
        convert_outputs=convert_transformer_outputs,
        grad_scaler=grad_scaler,
    )
    model.set_dim("nO", pytorch_model.config.hidden_size)
    model._layers = [transformer]


def forward(model: Model, X: Any, is_train: bool) -> tuple:
    tokenizer = model.attrs["tokenizer"]

    texts = [doc.text for doc in X]
    X = tokenizer(
        texts,
        add_special_tokens=False,
        return_token_type_ids=True,
        return_attention_mask=True,
        return_tensors="pt",
        padding=True,
        truncation=True,
    )
    Y, backward = model.layers[0](X, is_train)
    return Y, backward


def convert_transformer_outputs(
    model: Model, inputs_outputs: tuple, is_train: bool
) -> tuple:
    pad = model.ops.pad
    unpad = model.ops.unpad

    _, (Yt, Lt) = inputs_outputs

    def convert_for_torch_backward(dY: List[Floats2d]) -> ArgsKwargs:
        dY_t = xp2torch(pad(dY))
        return ArgsKwargs(args=(Yt,), kwargs={"grad_tensors": dY_t})  # type:ignore

    Y = cast(Floats3d, torch2xp(Yt))
    return (
        cast(List[Floats2d], unpad(Y, Lt.tolist())),
        convert_for_torch_backward,
    )

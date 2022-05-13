from typing import Callable, Iterable, List, Tuple

import srsly
from spacy import util
from spacy.errors import Errors
from spacy.language import Language
from spacy.pipeline import TrainablePipe
from spacy.tokens import Doc
from spacy.training import Example
from spacy.training.iob_utils import biluo_tags_to_spans, doc_to_biluo_tags
from spacy.vocab import Vocab
from thinc.config import Config
from thinc.model import Model
from thinc.optimizers import Optimizer
from thinc.types import Floats2d, Floats4d, Ints1d

from .aligners import Aligner
from .label_indexers import LabelIndexer
from .loss import ExpectedEntityRatioLoss


class PartialEntityRecognizer(TrainablePipe):
    def __init__(
        self,
        vocab: Vocab,
        model: Model,
        name: str,
        scorer: Callable,
        label_indexer: LabelIndexer,
        padding_index: int = -1,
        unknown_index: int = -100,
    ) -> None:
        self.vocab = vocab
        self.model = model
        self.name = name
        self.scorer = scorer
        self.label_indexer = label_indexer
        self.cfg: dict = {
            "labels": [],
            "tag_to_id": {},
            "id_to_tag": [],
            "outside_index": 0,
            "padding_index": padding_index,
            "unknown_index": unknown_index,
        }

    @property
    def labels(self) -> list:
        return self.cfg["labels"]

    @property
    def tag_to_id(self) -> dict:
        return self.cfg["tag_to_id"]

    @property
    def id_to_tag(self) -> list:
        return self.cfg["id_to_tag"]

    @property
    def outside_index(self) -> int:
        return self.cfg["outside_index"]

    @property
    def padding_index(self) -> int:
        return self.cfg["padding_index"]

    @property
    def unknown_index(self) -> int:
        return self.cfg["unknown_index"]

    def _get_lengths_from_docs(self, docs: List[Doc]) -> Ints1d:
        return self.model.ops.asarray1i([len(doc) for doc in docs])

    def predict(self, docs: List[Doc]) -> Tuple[Floats2d, List[Aligner]]:
        lengths = self._get_lengths_from_docs(docs)
        (_, guesses), aligners = self.model.predict((docs, lengths))
        return (guesses, aligners)

    def set_annotations(
        self,
        docs: List[Doc],
        batch_tag_indices_aligners: Tuple[Floats2d, List[Aligner]],
    ) -> None:
        batch_tag_indices, aligners = batch_tag_indices_aligners
        for doc, tag_indices, aligner in zip(
            docs, batch_tag_indices.tolist(), aligners
        ):
            tags = []
            for index in tag_indices:
                if index == self.padding_index:
                    break
                tags.append(self.id_to_tag[index])
            doc.ents = biluo_tags_to_spans(
                doc, aligner.from_subword(tags)
            )  # type:ignore

    def update(
        self,
        examples: Iterable[Example],
        *,
        drop: float = 0.0,
        sgd: Optimizer = None,
        losses: dict = None,
    ) -> dict:
        if losses is None:
            losses = {}
        losses.setdefault(self.name, 0.0)
        docs = [example.x for example in examples]
        lengths = self._get_lengths_from_docs(docs)
        ((log_potentials, _), aligners), backward = self.model.begin_update(
            (docs, lengths)
        )
        loss, grad = self.get_loss(examples, (log_potentials, aligners))
        # None is dummy gradients for aligners
        backward((grad, None))
        if sgd is not None:
            self.finish_update(sgd)
        losses[self.name] += loss
        return losses

    def initialize(
        self, get_examples: Callable, *, nlp: Language, labels: dict = None
    ) -> None:
        tag_to_id: dict = {"O": 0}
        id_to_tag: list = ["O"]
        X_small: List[Doc] = []
        for example in get_examples():
            if len(X_small) < 10:
                X_small.append(example.x)
            for tag in doc_to_biluo_tags(example.y):
                if tag not in tag_to_id:
                    id_to_tag.append(tag)
                    tag_to_id[tag] = len(tag_to_id)

        for tag in tag_to_id:
            if tag == "O":
                self.add_label(tag)
            else:
                self.add_label(tag.split("-")[1])

        self.model.initialize(
            X=(X_small, self._get_lengths_from_docs(X_small)),
            Y=({i: tag for i, tag in enumerate(id_to_tag)}, None),
        )

        self.cfg["tag_to_id"] = tag_to_id
        self.cfg["id_to_tag"] = id_to_tag
        self.cfg["outside_index"] = tag_to_id["O"]

    def get_loss(
        self,
        examples: Iterable[Example],
        scores_aligners: Tuple[Floats4d, List[Aligner]],
    ) -> Tuple[float, Floats4d]:
        scores, aligners = scores_aligners
        padding_index = self.padding_index
        unknown_index = self.unknown_index
        outside_index = self.outside_index
        loss_func = ExpectedEntityRatioLoss(padding_index, unknown_index, outside_index)
        batch_tags = []
        for example, aligner in zip(examples, aligners):
            tags = doc_to_biluo_tags(example.y)
            tags_aligned = aligner.to_subword(tags)
            batch_tags.append(tags_aligned)

        tag_indices = self.label_indexer(batch_tags, self.tag_to_id)
        truths = self.model.ops.asarray(tag_indices)  # type:ignore
        grad, loss = loss_func(scores, truths)  # type:ignore
        return loss.item(), grad  # type:ignore

    def add_label(self, label: str) -> int:
        if label in self.labels:
            return 0
        self.labels.append(label)
        self.vocab.strings.add(label)
        return 1

    def from_bytes(
        self, bytes_data: bytes, *, exclude: tuple = tuple()
    ) -> "PartialEntityRecognizer":

        self._validate_serialization_attrs()

        def load_model(b: bytes) -> None:
            try:
                self.model.from_bytes(b)
            except AttributeError:
                raise ValueError(Errors.E149) from None

        deserialize = {}
        if hasattr(self, "cfg") and self.cfg is not None:
            deserialize["cfg"] = lambda b: self.cfg.update(srsly.json_loads(b))
        deserialize["vocab"] = lambda b: self.vocab.from_bytes(  # type:ignore
            b, exclude=exclude
        )

        util.from_bytes(bytes_data, deserialize, exclude)
        self.model.initialize(
            Y=({i: tag for i, tag in enumerate(self.id_to_tag)}, None)
        )

        model_deserializers = {
            "model": lambda b: self.model.from_bytes(b),
        }
        util.from_bytes(bytes_data, model_deserializers, exclude)
        return self

    def from_disk(
        self, path: str, exclude: tuple = tuple()
    ) -> "PartialEntityRecognizer":
        self._validate_serialization_attrs()

        def load_model(p: str) -> None:
            try:
                with open(p, "rb") as mfile:
                    self.model.from_bytes(mfile.read())
            except AttributeError:
                raise ValueError(Errors.E149) from None

        deserialize = {}
        if hasattr(self, "cfg") and self.cfg is not None:
            deserialize["cfg"] = lambda p: self.cfg.update(srsly.read_json(p))
        deserialize["vocab"] = lambda p: self.vocab.from_disk(  # type:ignore
            p, exclude=exclude
        )
        util.from_disk(path, deserialize, exclude)
        self.model.initialize(
            Y=({i: tag for i, tag in enumerate(self.id_to_tag)}, None)
        )
        model_deserializers = {
            "model": load_model,
        }
        util.from_disk(path, model_deserializers, exclude)  # type:ignore
        return self


default_model_config = """
[model]
@architectures = "spacy-partial-tagger.PartialTagger.v1"
nI = 768
nO = null
dropout = 0.2
padding_index = -1

[model.misaligned_tok2vec]
@architectures = "spacy-partial-tagger.MisalignedTok2VecTransformer.v1"
model_name = "roberta-base"
"""
DEFAULT_NER_MODEL = Config().from_str(default_model_config)["model"]


@Language.factory(
    "partial_ner",
    assigns=["doc.ents", "token.ent_iob", "token.ent_type"],
    default_config={
        "model": DEFAULT_NER_MODEL,
        "scorer": {"@scorers": "spacy.ner_scorer.v1"},
        "label_indexer": {
            "@label_indexers": "spacy-partial-tagger.TransformerLabelIndexer.v1",
            "padding_index": -1,
            "unknown_index": -100,
        },
        "padding_index": -1,
        "unknown_index": -100,
    },
    default_score_weights={
        "ents_f": 1.0,
        "ents_p": 0.0,
        "ents_r": 0.0,
        "ents_per_type": None,
    },
)
def make_partial_ner(
    nlp: Language,
    name: str,
    model: Model,
    scorer: Callable,
    label_indexer: LabelIndexer,
    padding_index: int,
    unknown_index: int,
) -> PartialEntityRecognizer:
    return PartialEntityRecognizer(
        nlp.vocab, model, name, scorer, label_indexer, padding_index, unknown_index
    )

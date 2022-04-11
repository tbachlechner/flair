import itertools
from typing import Tuple, List, Set, Dict, Iterator, Sequence, NamedTuple, Union, Optional, Any

import torch

import flair
from flair.data import Dictionary, Sentence, Span, Relation, Label
from flair.embeddings import DocumentEmbeddings


class _RelationArgument(NamedTuple):
    """A `_RelationArgument` encapsulates either a relation's head or a tail span, including its label."""
    span: Span
    label: Label


# TODO: This closely shadows the RelationExtractor name. Maybe we need a better name here.
#  - EntityPairRelationClassifier ?
#  - MaskedRelationClassifier ?
class RelationClassifier(flair.nn.DefaultClassifier[Sentence]):

    def __init__(self,
                 document_embeddings: DocumentEmbeddings,
                 label_dictionary: Dictionary,
                 label_type: str,
                 entity_label_types: Union[str, Sequence[str], Dict[str, Optional[Set[str]]]],
                 relations: Optional[Dict[str, Set[Tuple[str, str]]]] = None,
                 zero_tag_value: str = 'O',
                 **classifierargs) -> None:
        """
        TODO: Add docstring
        # This does not yet support entities with two labels at the same span.
        # Supports only directional relations, self referencing relations are not supported
        :param document_embeddings:
        :param label_dictionary:
        :param label_type:
        :param entity_label_types:
        :param relations:
        :param classifierargs:
        """
        super().__init__(label_dictionary=label_dictionary,
                         final_embedding_size=document_embeddings.embedding_length,
                         **classifierargs)

        self.document_embeddings = document_embeddings

        self._label_type = label_type

        if isinstance(entity_label_types, str):
            self.entity_label_types: Dict[str, Optional[Set[str]]] = {entity_label_types: None}
        elif isinstance(entity_label_types, Sequence):
            self.entity_label_types: Dict[str, Optional[Set[str]]] = {entity_label_type: None
                                                                      for entity_label_type in entity_label_types}
        else:
            self.entity_label_types: Dict[str, Optional[Set[str]]] = entity_label_types

        self.relations = relations

        # Control mask templates
        self._head_mask: str = '[H-ENTITY]'
        self._tail_mask: str = '[T-ENTITY]'
        self.zero_tag_value = zero_tag_value

        # Auto-spawn on GPU, if available
        self.to(flair.device)

    def _entity_pair_permutations(self, sentence: Sentence) -> Iterator[Tuple[_RelationArgument, _RelationArgument]]:
        """
        Yields all valid entity pair permutations.
        The permutations are constructed by a filtered cross-product
        under the specifications of `self.entity_label_types` and `self.relations`.
        :param sentence: A flair `Sentence` object with entity annotations
        :return: Tuples of (<HEAD>, <TAIL>) `_RelationArguments`
        """
        entities: Iterator[_RelationArgument] = itertools.chain.from_iterable([  # Flatten nested 2D list
            (
                _RelationArgument(span=entity_span, label=entity_span.get_label(label_type=label_type))
                for entity_span in sentence.get_spans(type=label_type)
                # Only use entities labelled with the specified labels for each label type
                if labels is None or entity_span.get_label(label_type=label_type).value in labels
            )
            for label_type, labels in self.entity_label_types.items()
        ])

        # Yield head and tail entity pairs from the cross product of all entities
        for head, tail in itertools.product(entities, repeat=2):

            # Remove identity relation entity pairs
            if head.span is tail.span:
                continue

            # Remove entity pairs with labels that do not match any of the specified relations in `self.relations`
            if self.relations is not None and all((head.label.value, tail.label.value) not in pairs
                                                  for pairs in self.relations.values()):
                continue

            yield head, tail

    def _label_aware_head_mask(self, label: str) -> str:
        return self._head_mask.replace('ENTITY', label)

    def _label_aware_tail_mask(self, label: str) -> str:
        return self._tail_mask.replace('ENTITY', label)

    def _create_sentence_with_masked_spans(self, head: _RelationArgument, tail: _RelationArgument) -> Sentence:
        """
        Returns a new `Sentence` object with masked head and tail spans.
        The mask is constructed from the labels of the head and tail span.

        Example:
            For the `head=Google` and `tail=Larry Page` and
            the sentence "Larry Page and Sergey Brin founded Google .",
            the masked sentence is "[T-PER] and Sergey Brin founded [H-ORG]"

        :param head: The head `_RelationArgument`
        :param tail: The tail `_RelationArgument`
        :return: The masked sentence
        """
        original_sentence: Sentence = head.span.sentence
        assert original_sentence is tail.span.sentence, 'The head and tail need to come from the same sentence.'

        # We can not use the plaintext of the head/tail span in the sentence as the mask
        # since there may be multiple occurrences of the same entity mentioned in the sentence.
        # Therefore, we use the span's position in the sentence.
        masked_sentence_tokens: List[str] = []
        for token in original_sentence:

            if token is head.span[0]:
                masked_sentence_tokens.append(self._label_aware_head_mask(head.label.value))

            elif token is tail.span[0]:
                masked_sentence_tokens.append(self._label_aware_tail_mask(tail.label.value))

            elif (all(token is not non_leading_head_token for non_leading_head_token in head.span.tokens[1:]) and
                  all(token is not non_leading_tail_token for non_leading_tail_token in tail.span.tokens[1:])):
                masked_sentence_tokens.append(token.text)

        # TODO: Question: When I check the sentence with sentence.to_original_text(), the text is not consistently separated with whitespaces.
        #   Does the TextClassifier use the original text in any way?
        #   If not, I guess that only the tokens matter but not the whitespaces in between.
        return Sentence(masked_sentence_tokens)

    def _encode_sentence(self, sentence: Sentence) -> List[Tuple[Sentence, Relation]]:
        """
        Returns masked entity pair sentences and their relation for all valid entity pair permutations.
        The created masked sentences are newly created sentences with no reference to the passed sentence.
        The created relations have head and tail spans from the original passed sentence.

        Example:
            For the `founded_by` relation from `ORG` to `PER` and
            the sentence "Larry Page and Sergey Brin founded Google .",
            the masked sentences and relations are
            - "[T-PER] and Sergey Brin founded [H-ORG]" -> Relation(head='Google', tail='Larry Page')  and
            - "Larry Page and [T-PER] founded [H-ORG]"  -> Relation(head='Google', tail='Sergey Brin').

        :param sentence: A flair `Sentence` object with entity annotations
        :return: Encoded sentences and the corresponding relation in the original sentence
        """
        return [
            (self._create_sentence_with_masked_spans(head, tail), Relation(first=head.span, second=tail.span))
            for head, tail in self._entity_pair_permutations(sentence)
        ]

    def forward_pass(self,
                     sentences: Union[List[Sentence], Sentence],
                     for_prediction: bool = False) -> Union[Tuple[torch.Tensor, List[List[str]]],
                                                            Tuple[torch.Tensor, List[List[str]], List[Relation]]]:
        if not isinstance(sentences, list):
            sentences: List[Sentence] = [sentences]

        masked_sentence_embeddings: List[torch.Tensor] = []
        masked_sentence_batch_relations: List[Relation] = []
        gold_labels: List[List[str]] = []

        for sentence in sentences:
            # Encode the original sentence into a list of masked sentences and
            # the corresponding relations in the original sentence for all valid entity pair permutations.
            # Each masked sentence is one relation candidate.
            encoded: List[Tuple[Sentence, Relation]] = self._encode_sentence(sentence)

            # Process the encoded sentences, if there's at least one entity pair in the original sentence.
            if encoded:
                masked_sentences, relations = zip(*encoded)

                masked_sentence_batch_relations.extend(relations)

                # Embed masked sentences
                self.document_embeddings.embed(list(masked_sentences))
                encoded_sentence_embedding: torch.Tensor = torch.stack(
                    [masked_sentence.get_embedding(self.document_embeddings.get_names())
                     for masked_sentence in masked_sentences],
                    dim=0,
                )  # TODO: Should the embeddings be sent to flair.device or is this done later automatically?
                masked_sentence_embeddings.append(encoded_sentence_embedding)

                # Add gold labels for each masked sentence, if available.
                # Use a dictionary to find relation annotations for a given entity pair relation.
                relation_to_gold_label: Dict[str, str] = {
                    relation.unlabeled_identifier: relation.get_label(self.label_type,
                                                                      zero_tag_value=self.zero_tag_value).value
                    for relation in sentence.get_relations(self.label_type)
                }
                # TODO: The 'O' zero tag value is not part of the initial label dictionary. Is this fine?
                gold_labels.extend([
                    [relation_to_gold_label.get(relation.unlabeled_identifier, self.zero_tag_value)]
                    for relation in relations
                ])

        # TODO: Should the embeddings be sent to flair.device or is this done later automatically?
        # TODO: What should I return if the sentences contains no entity pairs? Is an empty tensor correct?
        masked_sentence_batch_embeddings: torch.Tensor = (
            torch.cat(masked_sentence_embeddings, dim=0) if masked_sentence_embeddings
            else torch.empty(0, self.document_embeddings.embedding_length)
        )
        if for_prediction:
            return masked_sentence_batch_embeddings, gold_labels, masked_sentence_batch_relations
        return masked_sentence_batch_embeddings, gold_labels

    def _get_state_dict(self) -> Dict[str, Any]:
        model_state: Dict[str, Any] = {
            **super()._get_state_dict(),
            'document_embeddings': self.document_embeddings,
            'label_dictionary': self.label_dictionary,
            'label_type': self.label_type,
            'entity_label_types': self.entity_label_types,
            'relations': self.relations,
            'zero_tag_value': self.zero_tag_value
        }
        return model_state

    @classmethod
    def _init_model_with_state_dict(cls, state: Dict[str, Any], **kwargs):
        return super()._init_model_with_state_dict(
            state,
            document_embeddings=state['document_embeddings'],
            label_dictionary=state['label_dictionary'],
            label_type=state['label_type'],
            entity_label_types=state['entity_label_types'],
            relations=state['relations'],
            zero_tag_value=state['zero_tag_value'],
            **kwargs
        )

    @property
    def label_type(self) -> str:
        return self._label_type
from typing import List

import torch
import torch.nn as nn
from torch.nn import CrossEntropyLoss, MSELoss

from transformers import BertForTokenClassification, BertPreTrainedModel, BertModel, BertTokenizerFast
from transformers.configuration_utils import PretrainedConfig

from os import path
import json
from random import choice

from ..layers.crf import ConditionalRandomField
from ..layers.loss import ContrastiveLoss, SingleLabelDotProductLoss

class DIETClassifierConfig(PretrainedConfig):
    def __init__(self, model: str, entities: List[str] = None, intents: List[str] = None, embedding_dimension: int = None, use_dot_product: bool = True):
        super().__init__()
        self.model = model
        self.entities = entities
        self.intents = intents
        self.embedding_dimension = embedding_dimension
        self.use_dot_product = use_dot_product
        self.hidden_dropout_prob = None
        self.hidden_size = None


class DIETClassifier(BertPreTrainedModel):
    def __init__(self, config: DIETClassifierConfig):
        """
        Create DIETClassifier model

        :param config: config for model
        """
        

        if path.exists(config.model):
            try:
                json_config = json.load(open(f"{config.model}/config.json", "r"))
            except Exception as ex:
                raise RuntimeError(f"Cannot load configuration fil from {config.model} by error: {ex}")

            try:
                checkpoint = torch.load(f"{config.model}/pytorch_model.bin")
            except Exception as ex:
                raise RuntimeError(f"Cannot load model from {config.model} by error: {ex}")

            pretrained_model = None
            config = PretrainedConfig.from_dict(json_config)
        else:
            pretrained_model = BertModel.from_pretrained(config.model)
            checkpoint = None
            if config.intents is None or config.entities is None:
                raise ValueError(f"Using pretrained from transformers should specific entities and intents")
            pretrained_model.config.update({"model": config.model, "entities": config.entities, "intents": config.intents, \
                                            "use_dot_product": config.use_dot_product, "embedding_dimension": config.embedding_dimension})
            
            config = pretrained_model.config

        super().__init__(config)
        if config.embedding_dimension is None: self.embedding_dimension = 128
        else: self.embedding_dimension = config.embedding_dimension
        
        self.use_dot_product = config.use_dot_product
        
        self.entities_list = ["O"] + config.entities
        self.num_entities = len(self.entities_list)
        self.intents_list = config.intents
        self.num_intents = len(self.intents_list)


        self.bert = BertModel(config, add_pooling_layer=False) if not pretrained_model else pretrained_model

        self.dropout = nn.Dropout(config.hidden_dropout_prob)

        # self.entities_classifier = nn.Linear(config.hidden_size, self.num_entities)
        self.entities_dense_embed = nn.Linear(config.hidden_size, self.num_entities)
        self.crf = ConditionalRandomField(self.num_entities)
        self.intents_output_embed = nn.Linear(config.hidden_size, self.embedding_dimension)
        self.intents_label_embed = nn.Embedding(self.num_intents+2,config.hidden_size)
        self.intents_label_dense = nn.Linear(config.hidden_size,self.embedding_dimension)

        self.intents_classifier = nn.Linear(config.hidden_size, self.num_intents)

        self.init_weights()

        if not pretrained_model:
            try:
                self.load_state_dict(checkpoint, strict=False)
            except Exception as ex:
                raise  RuntimeError(f"Cannot load state dict from checkpoint by error: {ex}")

    def embed_all_intent(self, num_intent:int, device: torch.device):
        """
        Embed all intent through Embed Layer

        :param num_intent:  the number of intents in vocabulary
        :param device:      device run training
        """
        all_intent = torch.LongTensor(list(range(0,self.num_intents))).to(device)
        embed_all_intent =  self.intents_label_dense(self.intents_label_embed(all_intent))

        return embed_all_intent
    def logit_intent(self, embed_out_intents: torch.Tensor, embed_all_intent: torch.Tensor):
        """
        Predict logit intent

        :param embed_out_intents:   embed intents of samples through model
        :param embed_all_intent:    embed all intent through Embed Layer
        """
        loss = SingleLabelDotProductLoss()
        cosin_list = []
        for out_intent in embed_out_intents:
            cosin_list.append(loss.sim(out_intent, embed_all_intent.float()))

        return torch.stack(cosin_list, dim = 0)

    def _neg_sample(self, num_intent:int, intent_labels: torch.Tensor,embed_all_intent: torch.Tensor):
        """
        generate negative sample

        :param num_intent:          the number of intents in vocabulary
        :param intent_labels:       positive labels
        :param embed_all_intent:    embed all intent through Embed Layer
        """
        all_label = torch.LongTensor(list(range(num_intent))).to(intent_labels.device)
        neg_label = []
        for sample in intent_labels.type(torch.LongTensor):
            neg_label.append(embed_all_intent[choice([label for label in all_label if label != sample])])

        return torch.stack(neg_label, dim = 0)

    def forward(
            self,
            input_ids=None,
            attention_mask=None,
            token_type_ids=None,
            position_ids=None,
            head_mask=None,
            inputs_embeds=None,
            intent_labels=None,
            entities_labels=None,
            output_attentions=None,
            output_hidden_states=None,
            return_dict=None,
    ):
        """
        training model if entities_labels and intent_labels are passed, else inference

        :param input_ids: embedding ids of tokens
        :param attention_mask: attention_mask
        :param token_type_ids: token_type_ids
        :param position_ids: position_ids (optional)
        :param head_mask: head_mask (optional)
        :param inputs_embeds: inputs_embeds (optional)
        :param intent_labels: labels of intent
        :param entities_labels: labels of entities
        :param output_attentions: return attention weight or not
        :param output_hidden_states: return hidden_states or not
        :param return_dict: return dictionary or not
        :return:
        """
        return_dict = return_dict if return_dict is not None else self.config.use_return_dict
        outputs = self.bert(
            input_ids,
            attention_mask=attention_mask,
            token_type_ids=token_type_ids,
            position_ids=position_ids,
            head_mask=head_mask,
            inputs_embeds=inputs_embeds,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            return_dict=return_dict,
        )

        sequence_output = outputs[0][:, 1:]
        sequence_output = self.dropout(sequence_output)

        pooled_output = outputs[0][:, :1]
        # pooled_output = outputs[1].unsqueeze(1)  #[CLS]
        pooled_output = self.dropout(pooled_output)
 
        entities_embed = self.entities_dense_embed(sequence_output)
           
        entities_loss = None
        if entities_labels is not None:
            entities_loss = -self.crf(entities_embed,entities_labels,attention_mask.bool())
            # if entities_loss < 0: entities_loss = 0
        entities_logits = self.crf.viterbi_tags(entities_embed)
        entities_logits = [path for path, _ in entities_logits]
        
        if self.use_dot_product:

            intent_output_embed = self.intents_output_embed(pooled_output)

            all_intent = self.embed_all_intent(num_intent= self.num_intents, device= intent_output_embed.device)
            intent_logits = self.logit_intent(intent_output_embed, all_intent)

            intent_loss = None
            if intent_labels is not None:
                intent_labels_embed = self.intents_label_embed(intent_labels)
                intent_labels_embed = self.intents_label_dense(intent_labels_embed)

                embed_neg_label  =self._neg_sample(num_intent= self.num_intents,intent_labels = intent_labels,embed_all_intent= all_intent)

                intent_loss_fct = SingleLabelDotProductLoss()
        
                intent_loss = intent_loss_fct(inputs_embed = intent_output_embed.view(-1, self.embedding_dimension),
                                                labels_embed_positive= intent_labels_embed,
                                                labels_embed_negative= embed_neg_label)
        else: 
            intent_logits = self.intents_classifier(pooled_output)
            
            intent_loss = None
            if intent_labels is not None:
                if self.num_intents == 1:
                    intent_loss_fct = MSELoss()
                    intent_loss = intent_loss_fct(intent_logits.view(-1), intent_labels.view(-1))
                else:
                    intent_loss_fct = CrossEntropyLoss()
                    intent_loss = intent_loss_fct(intent_logits.view(-1, self.num_intents), intent_labels.view(-1))


        if (entities_labels is not None) and (intent_labels is not None):
            loss = entities_loss * 0.5 + intent_loss * 0.5
        else:
            loss = None

        if self.training:
            return_dict = True

        if not return_dict:
            output = (loss,) + outputs[2:]
            return ((loss,) + output) if ((entities_loss is not None) and (intent_loss is not None)) else output

        return dict(
            entities_loss=entities_loss,
            intent_loss=intent_loss,
            loss=loss,
            logits=(entities_logits, intent_logits),
            hidden_states=outputs.hidden_states,
            attentions=outputs.attentions,
        )


if __name__ == '__main__':
    import os
    import sys

    sys.path.append(os.getcwd())

    from ..data_reader.data_reader import make_dataframe
    from ..data_reader.dataset import DIETClassifierDataset
    from transformers import AutoTokenizer

    files = ["dataset/nlu_QnA_converted.yml", "dataset/nlu_QnA_converted.yml"]
    tokenizer = AutoTokenizer.from_pretrained("dslim/bert-base-NER")

    df, entities_list, intents_list, synonym_dict = make_dataframe(files)
    dataset = DIETClassifierDataset(dataframe=df, tokenizer=tokenizer, entities=entities_list, intents=intents_list)

    config = DIETClassifierConfig(
        model="dslim/bert-base-NER",
        entities=entities_list,
        intents=intents_list
    )
    model = DIETClassifier(config=config)

    sentences = ["What if I'm late"]

    inputs = tokenizer(sentences, return_tensors="pt", padding="max_length", max_length=512)
    outputs = model(**{k: v for k, v in inputs.items()})

    print(outputs)

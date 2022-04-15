# Copyright (c) 2022, NVIDIA CORPORATION.
# SPDX-License-Identifier: Apache-2.0

# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import os
from operator import itemgetter
from omegaconf.dictconfig import DictConfig
from omegaconf import open_dict
from rdkit import Chem
from packaging import version

import torch
import torch.nn as nn
from pytorch_lightning.trainer.trainer import Trainer

import nemo
from nemo.collections.nlp.models.language_modeling.megatron_lm_encoder_decoder_model import MegatronLMEncoderDecoderModel
from nemo.utils import logging
from nemo.collections.nlp.modules.common.tokenizer_utils import get_nmt_tokenizer

from nemo_chem.tokenizer import MolEncTokenizer, DEFAULT_VOCAB_PATH, DEFAULT_MODEL_PATH
from nemo_chem.data import DatasetTypes, MoleculeEnumeration, build_train_valid_test_datasets
from nemo.collections.nlp.modules.common.megatron.utils import average_losses_across_data_parallel_group

try:
    from apex.transformer import tensor_parallel

    HAVE_APEX = True
except (ImportError, ModuleNotFoundError):
    HAVE_APEX = False

# Disable logging of invalid SMILES moloecules
from rdkit import RDLogger
lg = RDLogger.logger()
lg.setLevel(RDLogger.CRITICAL)

__all__ = ["MegaMolBARTModel"]

class MegaMolBARTModel(MegatronLMEncoderDecoderModel):
    """
    MegaMolBART pretraining
    """

    def __init__(self, cfg: DictConfig, trainer: Trainer):
        self._check_scheduler(cfg)
        super().__init__(cfg, trainer=trainer)

    def _check_scheduler(self, cfg):
        """Warn if maximum learning rate with Noam is less than minimum learning rate"""
        # TODO add to Noam Scheduler in NeMo
        if cfg.optim.sched.name == 'NoamAnnealing':
            if cfg.optim.sched.warmup_steps:
                warmup_steps = cfg.optim.sched.warmup_steps
            else:
                warmup_steps = int(cfg.optim.sched.warmup_ratio * cfg.optim.sched.max_steps)
            max_lr = cfg.optim.lr * cfg.optim.sched.d_model**(-0.5) * warmup_steps**(-0.5)
            min_lr = cfg.optim.sched.min_lr
            if max_lr <= min_lr:
                logging.warning(f'Warning: maximum learning rate for Noam Scheduler ({max_lr}) is less than minimum ({min_lr}).')
        return

    def _build_tokenizer(self):
        """
        Tokenizer from MegaMolBART.
        """
        # TODO set defaults in tokenizer once NeMo 1.8 conversion is complete
        vocab_path = self._cfg.tokenizer.get('vocab_path', DEFAULT_VOCAB_PATH)
        if not os.path.exists(vocab_path):
            raise ValueError(f'Vocab file not found at {vocab_path}')

        model_path = self._cfg.tokenizer.get('model_path', DEFAULT_MODEL_PATH)
        if not os.path.exists(model_path):
            raise ValueError(f'Model file not found at {model_path}')

        with open_dict(self._cfg):
            self._cfg.tokenizer.vocab_path = vocab_path
            self._cfg.tokenizer.model_path = model_path

        # TODO cleanup once NeMo 1.8 conversion is complete
        USE_NEMO_REGEX_TOKENIZER = version.parse(nemo.__shortversion__) >= version.parse('1.8')
        if USE_NEMO_REGEX_TOKENIZER:
            # TODO: use tokenizer config to define tokenizer
            self.tokenizer = get_nmt_tokenizer(
                library='regex',
                # include model files inside .nemo file
                tokenizer_model = self.register_artifact("tokenizer_model", model_path),
                vocab_file = self.register_artifact("vocab_file", vocab_path),
            )
        else:
            self.tokenizer = MolEncTokenizer.from_vocab_file(**self._cfg.tokenizer)

    def build_train_valid_test_datasets(self):
        logging.info('Building MegaMolBART datasets.')
        tensor_model_parallel_size = self._cfg.get('tensor_model_parallel_size', 1)

        global_batch_size = self.trainer.world_size * self._cfg.micro_batch_size / tensor_model_parallel_size
        eval_iters = (self.trainer.max_steps // self.trainer.val_check_interval + 1) * self.trainer.limit_val_batches
        test_iters = self.trainer.limit_test_batches
        train_valid_test_num_samples = [
            int(self.trainer.max_steps * global_batch_size),
            int(eval_iters * global_batch_size),
            int(test_iters * global_batch_size),
        ]

        if self._cfg.data.get('dataset_type', None) is not None:
            dataset_types = DatasetTypes.__members__
            if self._cfg.data.get('dataset_type') not in dataset_types:
                raise ValueError(f"dataset_type must be in {dataset_types}. Found {self._cfg.data.get('dataset_type')}")

        self._train_ds, self._validation_ds, self._test_ds = build_train_valid_test_datasets(
            self._cfg.data,
            self.trainer,
            train_valid_test_num_samples
        )

        logging.info(f'Length of train dataset: {len(self._train_ds)}')
        logging.info(f'Length of val dataset: {len(self._validation_ds)}')
        logging.info(f'Length of test dataset: {len(self._test_ds)}')
        logging.info(f'Finished building MegaMolBART datasets.')
        return self._train_ds, self._validation_ds, self._test_ds

    def build_pretraining_data_loader(self, dataset, consumed_samples):
        """Buld dataloader given an input dataset."""
        dataloader = super().build_pretraining_data_loader(dataset=dataset, consumed_samples=consumed_samples)
        
        # Add collate function and unpin memory to avoid crash with CUDA misaligned address
        # TODO remove when data loader complete
        dataloader.pin_memory = False
        pad_size_divisible_by_8 = True if self._cfg.masked_softmax_fusion else False
        dataloader.collate_fn = MoleculeEnumeration(tokenizer=self.tokenizer, 
                                                    seq_length=self._cfg.seq_length, 
                                                    pad_size_divisible_by_8=pad_size_divisible_by_8,
                                                    **self._cfg.data).collate_fn
        
        return dataloader

    def _eval_step(self, tokens_enc, tokens_dec, loss_mask, labels, enc_mask, dec_mask):
        ret_dict = self(tokens_enc, tokens_dec, enc_mask, dec_mask, tokentype_ids=None, lm_labels=labels,)
        tokens_loss = ret_dict['tokens_loss']
        loss = self.loss_func(loss_mask, tokens_loss)
        return loss, ret_dict

    def training_step(self, batch, batch_idx):
        tokens_enc, tokens_dec, loss_mask, labels, enc_mask, dec_mask = self.process_batch(batch)

        assert tokens_enc.max() < self.tokenizer.vocab_size, AssertionError('Encoder tokens are larger than vocabulary')
        assert tokens_dec.max() < self.tokenizer.vocab_size, AssertionError('Decoder tokens are larger than vocabulary')
        assert labels.max() < self.tokenizer.vocab_size, AssertionError('Label tokens are larger than vocabulary')

        loss, ret_dict = self._eval_step(tokens_enc=tokens_enc, tokens_dec=tokens_dec, loss_mask=loss_mask, 
                                         labels=labels, enc_mask=enc_mask, dec_mask=dec_mask)
        
        # cache reduced loss while accumulating gradients
        reduced_loss = average_losses_across_data_parallel_group([loss])
        self._reduced_loss_buffer.append(reduced_loss[0])

        if (batch_idx + 1) % self.trainer.accumulate_grad_batches == 0:
            # Reduced loss for logging.
            average_reduced_loss = sum(self._reduced_loss_buffer) / len(self._reduced_loss_buffer)
            lr = self._optimizer.param_groups[0]['lr']
            consumed_samples = self.compute_consumed_samples(self.trainer.global_step - self.init_global_step)

            logs = {'global_step': self.trainer.global_step,
                    'reduced_loss': average_reduced_loss,
                    'lr': lr,
                    'consumed_samples': consumed_samples}

            self.log_dict(logs)
            self._reduced_loss_buffer = []

        return loss

    def validation_step(self, batch, batch_idx):
        tokens_enc, tokens_dec, loss_mask, labels, enc_mask, dec_mask = self.process_batch(batch)
        loss, ret_dict = self._eval_step(tokens_enc=tokens_enc, tokens_dec=tokens_dec, loss_mask=loss_mask, 
                                         labels=labels, enc_mask=enc_mask, dec_mask=dec_mask)

        reduced_loss = average_losses_across_data_parallel_group([loss])
        target_smiles = batch['target_smiles']
        token_logits = ret_dict['token_logits']
        token_logits[:, :, self.tokenizer.vocab_size:] = -float('Inf') # never pick padded tokens
        metrics = self.calculate_metrics(token_logits=token_logits, loss_mask=loss_mask, labels=labels, 
                                         tokens_enc=tokens_enc, enc_mask=enc_mask, target_smiles=target_smiles)

        mode = 'val'
        logs = {
            'global_step': self.trainer.global_step,
            'reduced_loss': reduced_loss,
        }

        for metric_name, metric_value in metrics.items():
            logs[f'{mode}_{metric_name}'] = metric_value

        self.log_dict(logs)
        return reduced_loss

    def decode(self, tokens_enc, enc_mask, num_tokens_to_generate):
        # TODO: Revert to version from MegatonLMEncoderDecoderModel when sampling from padding tokens prohibited 
        encoder_hidden_states = itemgetter("enc_output")(
            self(
                encoder_input_ids=tokens_enc,
                decoder_input_ids=None,
                encoder_attn_mask=enc_mask,
                decoder_attn_mask=None,
                tokentype_ids=None,
                lm_labels=None,
                enc_hidden_states=None,
                output_enc_hidden_only=True,
            )
        )
        predicted_tokens_dec = (
            torch.LongTensor([self.tokenizer.bos_id] * tokens_enc.size(0)).unsqueeze(1).to(tokens_enc.device)
        )
        for _ in range(num_tokens_to_generate):
            dec_mask = predicted_tokens_dec != self.tokenizer.pad_id
            token_logits = itemgetter("token_logits")(
                self(
                    encoder_input_ids=tokens_enc,
                    decoder_input_ids=predicted_tokens_dec,
                    encoder_attn_mask=enc_mask,
                    decoder_attn_mask=dec_mask,
                    tokentype_ids=None,
                    lm_labels=None,
                    enc_hidden_states=encoder_hidden_states,
                    output_enc_hidden_only=False,
                )
            )
            token_logits = tensor_parallel.gather_from_tensor_model_parallel_region(token_logits)
            token_logits[:, :, self.tokenizer.vocab_size:] = -float('Inf') # never pick padded tokens
            log_probs, token_ids = torch.max(nn.functional.log_softmax(token_logits, dim=-1), dim=-1)
            predicted_tokens_dec = torch.cat([predicted_tokens_dec, token_ids[:, -1].unsqueeze(1)], 1)

        return predicted_tokens_dec, log_probs

    def sample_molecules(self, tokens_enc, enc_mask):
        """Autoregressively sample SMILES molecules from encoder hidden state

        Args:
            tokens_enc (torch.Tensor, long): token ID values for samples
            enc_mask (torch.Tensor, long): boolean mask for padded sections

        Returns:
            sampled_smiles (list[str]): a list of sampled SMILES strings
        """

        self.freeze()

        # Decode encoder hidden state to tokens
        predicted_tokens_ids, log_probs = self.decode(tokens_enc, enc_mask, self._cfg.max_position_embeddings)
        predicted_tokens_ids = predicted_tokens_ids.cpu().numpy().tolist()

        # Prune tokens by eos / padding and convert to SMILES
        for item, predicted_tokens_ in enumerate(predicted_tokens_ids):
            if self.tokenizer.eos_id in predicted_tokens_:
                idx = predicted_tokens_.index(self.tokenizer.eos_id)
                predicted_tokens_ids[item] = predicted_tokens_[:idx]
            else:
                # NB: this is slightly different from previous version in that pad tokens can be in the middle of sequence
                predicted_tokens_ids[item] = [id for id in predicted_tokens_ if id != self.tokenizer.pad_id]
            
        predicted_tokens_text = self.tokenizer.ids_to_tokens(predicted_tokens_ids)
        sampled_smiles = self.tokenizer.tokens_to_text(predicted_tokens_text)

        self.unfreeze()
        return sampled_smiles

    @staticmethod
    def calculate_character_accuracy(token_logits, loss_mask, labels):
        """Character (token) level accuracy

        Args:
            token_logits (torch.Tensor, float): softmax values for all tokens
            loss_mask (torch.Tensor, float): binary mask for ignored data (1=active, 0=mask), must be float
            labels (torch.Tensor, long): token IDs for correct output

        Returns:
            float: character accuracy value
        """

        # Get most probable token
        _, predicted_tokens = torch.max(token_logits, dim=2)
        correct_tokens = torch.eq(labels, predicted_tokens) * loss_mask # NB: mask includes EOS in calculation

        # Calculate percent of correct tokens
        num_correct = correct_tokens.sum().cpu().detach().item()
        total = loss_mask.sum().cpu().detach().item()
        character_accuracy = num_correct / total
        return character_accuracy

    def calculate_molecular_accuracy(self, tokens_enc, enc_mask, target_smiles):
        """Calculate molecular accuracy (with canonicalization)

        Args:
            tokens_enc (torch.Tensor, long): token ID values for samples
            enc_mask (torch.Tensor, long): boolean mask for padded sections
            target_smiles (str): ground truth for canonicalized SMILES

        Returns:
            float, float: molecular accuracy and percent invalid
        """
        sampled_smiles = self.sample_molecules(tokens_enc, enc_mask)
        sampled_mols = [Chem.MolFromSmiles(smi) for smi in sampled_smiles]
        invalid = [mol is None for mol in sampled_mols]

        canonical_smiles = ["Unknown" if mol is None else Chem.MolToSmiles(mol, canonical=True) for mol in sampled_mols]
        correct_smiles = [target_smiles[idx] == smi for idx, smi in enumerate(canonical_smiles)]

        num_correct = sum(correct_smiles)
        total = len(correct_smiles)
        num_invalid = sum(invalid)
        percent_invalid = num_invalid / total
        molecular_accuracy = num_correct / total

        return molecular_accuracy, percent_invalid

    def calculate_metrics(self, token_logits, loss_mask, labels, tokens_enc, enc_mask, target_smiles):
        """Calculate metrics for character accuracy, molecular accuracy, and invalid molecules

        Args:
            token_logits (torch.Tensor, float): softmax values for all tokens
            loss_mask (torch.Tensor, float): binary mask for ignored data (1=active, 0=mask), must be float
            labels (torch.Tensor, long): token IDs for correct output
            tokens_enc (torch.Tensor, long): token ID values for samples
            enc_mask (torch.Tensor, long): boolean mask for padded sections
            target_smiles (str): ground truth for canonicalized SMILES

        Returns:
            dict: dictionary of metric values
        """
        character_accuracy = self.calculate_character_accuracy(token_logits, loss_mask, labels)
        molecular_accuracy, percent_invalid = self.calculate_molecular_accuracy(tokens_enc, enc_mask, target_smiles)
        metrics = {'character_accuracy': character_accuracy,
                   'molecular_accuracy': molecular_accuracy,
                   'percent_invalid': percent_invalid}
        return metrics

    def list_available_models(self):
        pass
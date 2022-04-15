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

import torch
from nemo.utils import logging
from rdkit import Chem
import math
from pysmilesutils.augment import SMILESAugmenter
from typing import List
import numpy as np
import math

from nemo_chem.tokenizer import MolEncTokenizer


__all__ = ['MoleculeEnumeration']


class MoleculeEnumeration(object):
    def __init__(self, tokenizer: MolEncTokenizer, seq_length: int,
                encoder_augment: bool, encoder_mask: bool, 
                decoder_augment: bool, decoder_mask: bool, 
                canonicalize_input: bool, pad_size_divisible_by_8: bool, 
                **kwargs):
        self.tokenizer = tokenizer
        self.seq_length = seq_length
        self.encoder_augment = encoder_augment
        self.encoder_mask = encoder_mask
        self.decoder_augment = decoder_augment
        self.decoder_mask = decoder_mask
        self.canonicalize_input = canonicalize_input
        self.pad_size_divisible_by_8 = pad_size_divisible_by_8 # workaround for CUDA alignment bug
        # self.aug = CanonicalSMILESAugmenter().randomize_mol_restricted

    def _smiles_augmeter_func(self, smiles: str, augment_data: bool):
        """Regularize SMILES by coverting to RDKit mol objects and back

        Args:
            smiles (str): Input SMILES from dataset
            canonicalize_input (bool, optional): Canonicalize by default. Defaults to False.
            smiles_augmenter: Function to augment/randomize SMILES. Defaults to None
        """
        mol = Chem.MolFromSmiles(smiles)
        canon_smiles = Chem.MolToSmiles(mol, canonical=True) if self.canonicalize_input else smiles

        if augment_data:
            # aug_mol = self.aug(mol)
            atom_order: List[int] = list(range(mol.GetNumAtoms()))
            np.random.shuffle(atom_order)
            aug_mol = Chem.RenumberAtoms(mol, atom_order) # TODO how to use PySMILESutils for this

            # There is a very rare possibility that RDKit will not be able to generate 
            # the SMILES for the augmented mol. In this case we just use the canonical 
            # mol to generate the SMILES
            try:
                aug_smiles = Chem.MolToSmiles(aug_mol, canonical=False)
            except RuntimeError:
                logging.info(f'Could not generate smiles for {smiles} after augmenting. Forcing canonicalization')
                aug_smiles = canon_smiles if self.canonicalize_input else Chem.MolToSmiles(mol, canonical=True)
        else:
            aug_smiles = Chem.MolToSmiles(mol, canonical=False)

        assert len(aug_smiles) > 0, AssertionError('Augmented SMILES string is empty')
        assert len(canon_smiles) > 0, AssertionError('Canonical SMILES string is empty')
        return aug_smiles, canon_smiles

    def _check_seq_len(self, tokens: List[List[str]], mask: List[List[int]]):
        """ Warn user and shorten sequence if the tokens are too long, otherwise return original

        Args:
            tokens (List[List[str]]): List of token sequences
            mask (List[List[int]]): List of mask sequences

        Returns:
            tokens (List[List[str]]): List of token sequences (shortened, if necessary)
            mask (List[List[int]]): List of mask sequences (shortened, if necessary)
        """

        seq_len = max([len(ts) for ts in tokens])
        if seq_len > self.seq_length:
            tokens_short = [ts[:self.seq_length] for ts in tokens]
            mask_short = [ms[:self.seq_length] for ms in mask]
            return (tokens_short, mask_short)
        return (tokens, mask)

    def _prepare_tokens(self, batch: List[str], augment_data: bool, mask_data: bool = False):
        """Prepare tokens for encoder or decoder from batch of input SMILES strings

        Args:
            batch (List[str]): Batch of input SMILES strings
            augment_data (bool): Augment SMILES
            mask_data (bool, optional): Mask decoder tokens. Defaults to False.

        Returns:
            dict: token output
        """
        # Perform augmentation
        smiles_list = [self._smiles_augmeter_func(smiles, augment_data=augment_data) for smiles in batch]
        smiles = [x[0] for x in smiles_list]
        canon_targets = [x[1] for x in smiles_list]

        # Tokenize with optional masking, padding is done later due to differences in encoder/decoder bos/eos tokens
        token_output = self.tokenizer.tokenize(smiles, pad=False, mask=mask_data)

        if mask_data:
            tokens = token_output['masked_tokens']
            mask = token_output['token_masks']
        else:
            tokens = token_output['original_tokens']
            mask = [[True] * len(ts) for ts in tokens]  # 1/True = Active, 0/False = Inactive

        # Verify sequence length
        tokens, mask = self._check_seq_len(tokens, mask)

        token_output = {
            "tokens": tokens,
            "mask": mask,
            "target_smiles": canon_targets
        }

        return token_output

    def _pad_seqs(self, seqs, pad_token):
        pad_length = max([len(seq) for seq in seqs])
        if self.pad_size_divisible_by_8:
            pad_length = int(math.ceil(pad_length/8) * 8)

        padded = [seq + ([pad_token] * (pad_length - len(seq))) for seq in seqs]
        masks = [([1] * len(seq)) + ([0] * (pad_length - len(seq))) for seq in seqs] # 1/True = Active, 0/False = Inactive
        return padded, masks

    def collate_fn(self, batch: List[str], label_pad: int = -1):
        """Collate function for NeMo MegaMolBART. Format of data has been altered for NeMo per 'NB' comments. 
        This code should be cleaned up and validated once new tokenizer from NeMo is incorporated."""

        # Dimensions required by NeMo: [batch, sequence + padding] 
        # Encoder
        encoder_dict = self._prepare_tokens(batch, augment_data=self.encoder_augment, mask_data=self.encoder_mask)
        encoder_tokens = encoder_dict['tokens'] # TODO boolean masks are never used from this function -- remove during refactor

        enc_token_ids = self.tokenizer.convert_tokens_to_ids(encoder_tokens)
        enc_token_ids, encoder_mask = self._pad_seqs(enc_token_ids, self.tokenizer.pad_id)
        
        enc_token_ids = torch.tensor(enc_token_ids, dtype=torch.int64)
        encoder_mask = torch.tensor(encoder_mask, dtype=torch.int64)

        # Decoder
        decoder_dict = self._prepare_tokens(batch, augment_data=self.decoder_augment, mask_data=self.decoder_mask)
        decoder_tokens = decoder_dict['tokens']

        dec_token_ids = self.tokenizer.convert_tokens_to_ids(decoder_tokens)

        label_ids = [example + [self.tokenizer.eos_id] for example in dec_token_ids] # assign label_ids before adding bos_id to decoder
        dec_token_ids = [[self.tokenizer.bos_id] + example for example in dec_token_ids]
        dec_token_ids, decoder_mask = self._pad_seqs(dec_token_ids, self.tokenizer.pad_id)

        dec_token_ids = torch.tensor(dec_token_ids, dtype=torch.int64)
        decoder_mask = torch.tensor(decoder_mask, dtype=torch.int64)
        
        label_token_ids, loss_mask = self._pad_seqs(label_ids, self.tokenizer.pad_id)
        label_token_ids = torch.tensor(label_token_ids, dtype=torch.int64)
        loss_mask = torch.tensor(loss_mask, dtype=torch.int64)
        label_token_ids[~loss_mask.to(torch.bool)] = label_pad
        
        collate_output = {'text_enc': enc_token_ids,
                          'enc_mask': encoder_mask,
                          'text_dec': dec_token_ids,
                          'dec_mask': decoder_mask,
                          'labels': label_token_ids, 
                          'loss_mask': loss_mask,
                          'target_smiles': encoder_dict['target_smiles']} # smiles strings

        return collate_output
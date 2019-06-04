from .configuration import SampleRNNConfiguration
from .execution import SampleRNNExecution
from .utils import SampleRNNQuantizer, SampleRNNLabReader
from .data import SampleRNNData
from typing import List
import pickle
import soundfile
import torch
from torch.utils.data import Dataset
import numpy as np
import os
import ahoproc_tools
import random
import operator


class SampleRNNDataset(Dataset):
    """

    """

    conf: SampleRNNConfiguration
    quantizer: SampleRNNQuantizer
    data: SampleRNNData

    is_adaptation: bool
    split: str

    conds_linguistic_categories = {
        'phonemes': None,
        'vowels': None,
        'gpos': None,
        'tobi': None
    }

    normalize_conds: bool
    return_full_utterance: bool

    speakers_ids = None
    utterances_ids = None

    pase_seed_duration = 60

    def __init__(self, execution: SampleRNNExecution, quantizer: SampleRNNQuantizer, normalize_conds: bool,
                 is_adaptation: bool, split: str):

        super().__init__()
        self.execution = execution
        self.conf = execution.experiment.conf
        self.quantizer = quantizer
        self.data = execution.experiment.data

        self.is_adaptation = is_adaptation
        self.split = split

        self.conds_linguistic_categories = {
            'phonemes': sorted(list(self.data.utterances_conds_linguistic_categories['phonemes'])),
            'vowels': sorted(list(self.data.utterances_conds_linguistic_categories['vowels'])),
            'gpos': sorted(list(self.data.utterances_conds_linguistic_categories['gpos'])),
            'tobi': sorted(list(self.data.utterances_conds_linguistic_categories['tobi']))
        }

        self.normalize_conds = normalize_conds
        self.return_full_utterance = execution.command not in ['train']

        # Lala
        if not is_adaptation and split == 'train':
            self.speakers_ids = self.data.modeling_speakers_ids
            self.utterances_ids = execution.experiment.data.modeling_utterances_ids_train
        elif not is_adaptation and split == 'validation':
            self.speakers_ids = self.data.modeling_speakers_ids
            self.utterances_ids = execution.experiment.data.modeling_utterances_ids_val
        elif not is_adaptation and split == 'test':
            self.speakers_ids = self.data.modeling_speakers_ids
            self.utterances_ids = execution.experiment.data.modeling_utterances_ids_test
        elif is_adaptation and split == 'train':
            self.speakers_ids = self.data.adaptation_speakers_ids
            self.utterances_ids = execution.experiment.data.adaptation_utterances_ids_train
        elif is_adaptation and split == 'validation':
            self.speakers_ids = self.data.adaptation_speakers_ids
            self.utterances_ids = execution.experiment.data.adaptation_utterances_ids_val
        elif is_adaptation and split == 'test':
            self.speakers_ids = self.data.adaptation_speakers_ids
            self.utterances_ids = execution.experiment.data.adaptation_utterances_ids_test

    def __getitem__(self, item):
        # Get the information objects
        utterance = self.data.utterances_info[self.utterances_ids[item]]
        speaker = self.data.speakers_info[utterance['speaker_id']]
        dataset = self.data.datasets_info[speaker['dataset_id']]

        # Load the differente utterance files
        utterance_read_wav, _ = soundfile.read(dataset['wavs_folder_path'] + utterance['path'] + '.wav')

        # Get cut lengths
        utterance_wav_len_model, utterance_conds_len_model = self._get_model_len(
            utterance_wav_len_real=utterance_read_wav.shape[0]
        )

        # Pad the WAV if neccesary
        utterance_wav_len = min(utterance_wav_len_model, utterance_read_wav.shape[0])
        utterance_wav = np.zeros((utterance_wav_len_model))
        utterance_wav[:utterance_wav_len] = utterance_read_wav[:utterance_wav_len]

        # Speaker cond_type
        speaker_conds = np.zeros(self.conf.conditionants['speaker_size'])
        if self.conf.conditionants['speaker_type'] == 'pase_seed':
            speaker_conds = self._get_speaker_conds_pase_seed(dataset, speaker, utterance)

        # Acoustic cond_type
        if self.conf.conditionants['utterance_type'] == 'acoustic':
            utterance_conds_acoustic = self._get_utterance_conds_acoustic(dataset, speaker, utterance)
            utterance_conds_len = min(utterance_conds_len_model, utterance_conds_acoustic.shape[0])
            utterance_conds = np.zeros((utterance_conds_len_model, self.conf.conditionants['utterance_size']))
            utterance_conds[:utterance_conds_len, :] = utterance_conds_acoustic[:utterance_conds_len, :]

        # Linguistic cond_type
        elif self.conf.conditionants['utterance_type'] == 'linguistic':
            utterance_conds_linguistic = self._get_utterance_conds_linguistic(dataset, speaker, utterance)
            utterance_conds_len = min(utterance_conds_len_model, utterance_conds_linguistic.shape[0])
            utterance_conds = np.zeros((utterance_conds_len_model, self.conf.conditionants['utterance_size']))
            utterance_conds[:utterance_conds_len, :] = utterance_conds_linguistic[:utterance_conds_len, :]

        # Linguistic + LogF0 + VU
        elif self.conf.conditionants['utterance_type'] == 'linguistic_lf0':
            utterance_linguistic_conds_tensor = self._get_utterance_conds_linguistic(dataset, speaker, utterance)
            utterance_acoustic_conds_tensor = self._get_utterance_conds_acoustic(dataset, speaker, utterance)
            utterance_conds_len = max(utterance_conds_len_model, utterance_linguistic_conds_tensor.shape[0],
                                      utterance_acoustic_conds_tensor.shape[0])
            utterance_conds = np.zeros((utterance_conds_len, self.conf.conditionants['utterance_size']))
            utterance_conds[:utterance_linguistic_conds_tensor.shape[0], :self.conf.conditionants['utterance_size'] -
                                                                          2] = utterance_linguistic_conds_tensor
            utterance_conds[:utterance_acoustic_conds_tensor.shape[0], self.conf.conditionants['utterance_size'] - 2:] \
                = utterance_acoustic_conds_tensor[:, -2:]

        # Invalid cond_type
        else:
            self.execution.experiment.logger.error('Invalid Conds Type')
            exit()

        # Append frame_size samples at the begining of the WAV
        utterance_wav = np.concatenate([np.zeros(self.conf.architecture['frame_size']), utterance_wav])

        # PATCH UTT LENGTH
        utterance['wav_len'] = utterance_read_wav.shape[0]

        # Return the values
        return utterance_wav, utterance_conds, speaker_conds, \
               {'dataset': dataset, 'speaker': speaker, 'utterance': utterance}

    def __len__(self):
        return len(self.utterances_ids)

    def shuffle_utterances(self):
        random.shuffle(self.utterances_ids)

    def get_item_paths(self, item):
        utterance = self.data.utterances_info[self.utterances_ids[item]]
        speaker = self.data.speakers_info[utterance['speaker_id']]
        dataset = self.data.datasets_info[speaker['dataset_id']]
        return {
            'wav': dataset['wavs_folder_path'] + utterance['path'] + '.wav',
            'acoustic_conds': {
                'cc': dataset['conds_utterance']['acoustic_folder_path'] + utterance['path'] + '.cc',
                'fv': dataset['conds_utterance']['acoustic_folder_path'] + utterance['path'] + '.fv',
                'lf0': dataset['conds_utterance']['acoustic_folder_path'] + utterance['path'] + '.lf0'
            },
            'linguistic_conds': dataset['conds_utterance']['linguistic_folder_path'] + utterance['path'] + '.lab'
        }

    def set_pase_seed_duration(self, pase_seed_duration):
        self.pase_seed_duration = pase_seed_duration

    def _get_model_len(self, utterance_wav_len_real: int):
        # Compute samples in every forward
        samples_per_forward = self.conf.architecture['frame_size'] * self.conf.architecture['sequence_length']

        # If full_utterance flag is not set, cut the utterance
        if not self.return_full_utterance:
            next_seq_length_mult = int((utterance_wav_len_real // samples_per_forward) * samples_per_forward)

        # If set, then prepare to fill with zeros
        else:
            next_seq_length_mult = int(((utterance_wav_len_real // samples_per_forward) + 1) * samples_per_forward)

        # Return both results
        return next_seq_length_mult, int(next_seq_length_mult / self.conf.architecture['frame_size'])

    def _get_speaker_conds_pase_seed(self, dataset, speaker, utterance):

        # Create placeholder for the PASE seed path
        pase_seed_path = None

        # Format the url of the PASE folder
        if self.pase_seed_duration == 1:
            pase_seed_path = dataset['conds_speaker']['pase_seed_folder_path'][:-1] + '_1s' + os.sep + speaker['name'] \
                             + '.seed.pkl'
        elif self.pase_seed_duration == 10:
            pase_seed_path = dataset['conds_speaker']['pase_seed_folder_path'][:-1] + '_10s' + os.sep + speaker[
                'name'] + '.seed.pkl'
        elif self.pase_seed_duration == 60:
            pase_seed_path = dataset['conds_speaker']['pase_seed_folder_path'][:-1] + '_60s' + os.sep + speaker['name'] \
                             + '.seed.pkl'
        elif self.pase_seed_duration == 120:
            pase_seed_path = dataset['conds_speaker']['pase_seed_folder_path'][:-1] + '_120s' + os.sep + speaker[
                'name'] + '.seed.pkl'
        with open(pase_seed_path, 'rb') as speaker_conds_file:
            return pickle.load(speaker_conds_file)

    def _get_utterance_conds_acoustic(self, dataset, speaker, utterance):
        """

        Args:
            utterance_conds_acoustic_info:

        Returns:
        """

        # Load Acoustic Conds
        utterance_cc = ahoproc_tools.io.read_aco_file(
            dataset['conds_utterance']['acoustic_folder_path'] + utterance['path'] + '.cc', (-1, 40))
        utterance_fv = ahoproc_tools.io.read_aco_file(
            dataset['conds_utterance']['acoustic_folder_path'] + utterance['path'] + '.fv', (-1,))
        utterance_lf0 = ahoproc_tools.io.read_aco_file(
            dataset['conds_utterance']['acoustic_folder_path'] + utterance['path'] + '.lf0', (-1,))

        # Interpolate FV and LF0, obtain VU
        utterance_fv, _ = ahoproc_tools.interpolate.interpolation(utterance_fv, 1e3)
        utterance_lf0, utterance_vu = ahoproc_tools.interpolate.interpolation(utterance_lf0, -1e10)

        # Log(FV)
        utterance_fv = np.log(utterance_fv)

        # Join
        utterance_conds = np.concatenate([
            utterance_cc,
            np.expand_dims(utterance_fv, 1),
            np.expand_dims(utterance_lf0, 1),
            np.expand_dims(utterance_vu, 1)
        ], axis=1)

        # Normalize conditionants to have 0 mean and 1 std
        utterance_conds = (utterance_conds - speaker['conds_acoustic_stads'][0]) / speaker['conds_acoustic_stads'][1]

        # Return the Tensor
        return utterance_conds

    def _get_utterance_conds_linguistic(self, dataset, speaker, utterance):

        # Load Linguistic Conds
        utterance_conds = None

        # Iterate over each line of the .lab file
        for lab_line in SampleRNNLabReader.read_lab(
                lab_file_path=dataset['conds_utterance']['linguistic_folder_path'] + utterance['path'] + '.lab'):
            # Tuple to numpy
            lab_line = np.asarray(lab_line)

            # Substitute ABSOLUTE DURATION in the 0 index
            lab_line[0] = int(lab_line[1]) - int(lab_line[0])

            # Substitute CATEGORICAL conditionants with the index
            lab_line[2] = self.conds_linguistic_categories['phonemes'].index(lab_line[2])
            lab_line[3] = self.conds_linguistic_categories['phonemes'].index(lab_line[3])
            lab_line[4] = self.conds_linguistic_categories['phonemes'].index(lab_line[4])
            lab_line[5] = self.conds_linguistic_categories['phonemes'].index(lab_line[5])
            lab_line[6] = self.conds_linguistic_categories['phonemes'].index(lab_line[6])
            lab_line[27] = self.conds_linguistic_categories['vowels'].index(lab_line[27])
            lab_line[31] = self.conds_linguistic_categories['gpos'].index(lab_line[31])
            lab_line[33] = self.conds_linguistic_categories['gpos'].index(lab_line[33])
            lab_line[41] = self.conds_linguistic_categories['gpos'].index(lab_line[41])
            lab_line[49] = self.conds_linguistic_categories['tobi'].index(lab_line[49])

            # Substitute unknowns with 0s and change dtype
            lab_line[lab_line == 'x'] = 0
            lab_line = lab_line.astype(np.float)

            # Compute the number of conds to fill
            steps_n = (lab_line[0] * 10E-5) / 5

            # Normalize conds
            lab_line = (lab_line - speaker['conds_linguistic_stads'][0]) / speaker['conds_linguistic_stads'][1]

            # Copy the vector steps_n times
            utterance_ling = np.repeat(np.expand_dims(lab_line, 0), steps_n, axis=0)

            # Create linspace vector for relative duration between 0 and 1
            utterance_ling[:, 1] = np.linspace(0, 1, num=steps_n)

            # Append the result to the global tensor (remove the first two features)
            if utterance_conds is None:
                utterance_conds = utterance_ling
            else:
                utterance_conds = np.concatenate([utterance_conds, utterance_ling])

        # Return the Tensor
        return utterance_conds

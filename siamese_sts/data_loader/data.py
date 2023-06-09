import pandas as pd
from siamese_sts.data_loader.preprocess import Preprocess
import logging
import torch
from siamese_sts.data_loader.dataset import STSDataset
from datasets import load_dataset
import torchtext
from torchtext.data.utils import get_tokenizer

logging.basicConfig(level=logging.INFO)

"""
For loading STS data loading and preprocessing
"""


class STSData:
    def __init__(
        self,
        dataset_name,
        columns_mapping,
        stopwords_path="siamese_sts/data_loader/stopwords-en.txt",
        model_name="lstm",
        max_sequence_len=512,
        pretrained_model_name="prajjwal1/bert-mini",
        normalization_const=5.0,
        normalize_labels=False,
        prevocab=lambda x: x,
        vector_cache='.vector_cache'
    ):
        """
        Loads data into memory and create vocabulary from text field.
        """
        self.normalization_const = normalization_const
        self.normalize_labels = normalize_labels
        self.pretrained_model_name = pretrained_model_name
        self.model_name = model_name
        self.max_sequence_len = max_sequence_len
        self.dataset_name = dataset_name
        ## load data file into memory
        self.load_data(dataset_name, columns_mapping, stopwords_path)
        self.columns_mapping = columns_mapping
        ## create vocabulary over entire dataset before train/test split
        
        self.train_set = prevocab(self.train_set)
        self.val_set = prevocab(self.val_set)
        self.test_set = prevocab(self.test_set)
        
        self.create_vocab(vector_cache)

    def load_data(self, dataset_name, columns_mapping, stopwords_path):
        """
        Reads data set file from disk to memory using pandas
        """
        logging.info("loading and preprocessing data...")
        ## load datasets
        train_set = pd.DataFrame(load_dataset(dataset_name, split="train"))
        val_set = pd.DataFrame(load_dataset(dataset_name, split="validation"))
        test_set = pd.DataFrame(load_dataset(dataset_name, split="test"))
        ## init preprocessor
        preprocessor = Preprocess(stopwords_path)
        ## performing text preprocessing
        self.train_set = preprocessor.perform_preprocessing(train_set, columns_mapping)
        self.val_set = preprocessor.perform_preprocessing(val_set, columns_mapping)
        self.test_set = preprocessor.perform_preprocessing(test_set, columns_mapping)
        logging.info("reading and preprocessing data completed...")

    def create_vocab(self, vector_cache):
        """
        Creates vocabulary over entire text data field.
        """
        logging.info("creating vocabulary...")
        self.train_set["concat_text"] = (
            self.train_set["clean_sent1"] + " " + self.train_set["clean_sent2"]
        )
        ## init tokenizer
        self.en_tokenizer = get_tokenizer("spacy", language="en_core_web_sm")

        # get the vocab instance
        self.vocab = torchtext.vocab.FastText(cache=vector_cache)
        logging.info("creating vocabulary completed...")

    def data2tensors(self, data):
        """
        Converts raw data sequences into vectorized sequences as tensors
        """
        (
            vectorized_sents_1,
            vectorized_sents2,
            sents1_lengths,
            sents2_lengths,
            targets,
        ) = ([], [], [], [], [])
        raw_sents_1 = list(data.clean_sent1.values)
        raw_sents_2 = list(data.clean_sent2.values)

        ## get the text sequence from dataframe
        for index, (sentence_1, sentence_2) in enumerate(zip(raw_sents_1, raw_sents_2)):

            ## convert sentence into vectorized form replacing words with vocab indices
            vectorized_sent_1 = self.vectorize_sequence(sentence_1)
            vectorized_sent_2 = self.vectorize_sequence(sentence_2)

            ## computing sequence lengths for padding
            sequence_1_length = len(vectorized_sent_1)
            sequence_2_length = len(vectorized_sent_2)

            if sequence_1_length <= 0 or sequence_2_length <= 0:
                continue

            ## adding sequence vectors to train matrix
            vectorized_sents_1.append(vectorized_sent_1)
            sents1_lengths.append(sequence_1_length)

            vectorized_sents2.append(vectorized_sent_2)
            sents2_lengths.append(sequence_2_length)

            ## fetching label for this example
            targets.append(data[self.columns_mapping["label"]].values[index])

        if self.normalize_labels:
            targets = [target / self.normalization_const for target in targets]

        ## padding zeros at the end of tensor till max length tensor
        padded_sent1_tensor = self.pad_sequences(
            vectorized_sents_1, torch.tensor(sents1_lengths, dtype=torch.int64)
        )
        padded_sent2_tensor = self.pad_sequences(
            vectorized_sents2, torch.tensor(sents2_lengths, dtype=torch.int64)
        )
        sents1_length_tensor = torch.tensor(sents1_lengths, dtype=torch.int64)  ## casting to long
        sents2_length_tensor = torch.tensor(sents2_lengths, dtype=torch.int64)  ## casting to long
        target_tensor = torch.tensor(targets, dtype=torch.float32)  ## casting to long

        return (
            padded_sent1_tensor,
            padded_sent2_tensor,
            target_tensor,
            sents1_length_tensor,
            sents2_length_tensor,
            raw_sents_1,
            raw_sents_2,
        )

    def get_data_loader(self, batch_size=8):
        sts_dataloaders = {}
        for split_name, data in [
            ("train_loader", self.train_set),
            ("val_loader", self.val_set),
            ("test_loader", self.test_set),
        ]:
            (
                padded_sent1_tensor,
                padded_sent2_tensor,
                target_tensor,
                sents1_length_tensor,
                sents2_length_tensor,
                raw_sents_1,
                raw_sents_2,
            ) = self.data2tensors(data)
            self.sts_dataset = STSDataset(
                padded_sent1_tensor,
                padded_sent2_tensor,
                target_tensor,
                sents1_length_tensor,
                sents2_length_tensor,
                raw_sents_1,
                raw_sents_2,
            )
            sts_dataloaders[split_name] = torch.utils.data.DataLoader(
                self.sts_dataset, batch_size=batch_size
            )

        return sts_dataloaders

    def sort_batch(self, batch, targets, lengths):
        """
        Sorts the data, lengths and target tensors based on the lengths
        of the sequences from longest to shortest in batch
        """
        sents1_lengths, perm_idx = lengths.sort(0, descending=True)
        sequence_tensor = batch[perm_idx]
        target_tensor = targets[perm_idx]
        return sequence_tensor.transpose(0, 1), target_tensor, sents1_lengths

    def vectorize_sequence(self, sentence):
        """
        Replaces tokens with their indices in vocabulary
        """
        return [self.vocab.stoi[token.lower()] if token.lower() in self.vocab.stoi else 0 for token in self.en_tokenizer(sentence)]

    def pad_sequences(self, vectorized_sents_1, sents1_lengths):
        """
        Pads zeros at the end of each sequence in data tensor till max
        length of sequence in that batch
        """
        max_len = self.max_sequence_len
        if self.model_name == "lstm":
            max_len = sents1_lengths.max()

        padded_sequence_tensor = torch.zeros(
            (len(vectorized_sents_1), max_len)
        ).long()  ## init zeros tensor
        for idx, (seq, seqlen) in enumerate(
            zip(vectorized_sents_1, sents1_lengths)
        ):  ## iterate over each sequence
            padded_sequence_tensor[idx, :seqlen] = torch.tensor(
                seq,
                dtype=torch.int64
            )  ## each sequence get padded by zeros until max length in that batch
        return padded_sequence_tensor  ## returns padded tensor

import logging
from math import inf
from typing import List, Union, Optional

import torch
from torch import nn

import flair.embeddings
import flair.nn
from flair.data import Sentence, Dictionary, Corpus
from flair.datasets import DataLoader, SentenceDataset
from flair.training_utils import store_embeddings

log = logging.getLogger("flair")


class Lemmatizer(flair.nn.Classifier):

    def __init__(self,
                 embeddings: flair.embeddings.TokenEmbeddings = None,
                 label_type: str = 'lemma',
                 beam_size: int = 5,
                 rnn_input_size: int = 30,
                 rnn_hidden_size: int = 128,
                 rnn_layers: int = 1,
                 encode_characters: bool = True,
                 use_attention: bool = True,
                 char_dict: Union[str, Dictionary] = "common-chars-lemmatizer",
                 max_sequence_length_dependent_on_input: bool = True,
                 max_sequence_length: int = 20,
                 padding_in_front_for_encoder: bool = False,
                 start_symbol_for_encoding: bool = True,
                 end_symbol_for_encoding: bool = False,
                 batching_in_rnn: bool = True,
                 bidirectional_encoding: bool = False
                 ):
        """
        Initializes a Lemmatizer model
        The model consists of a decoder and an encoder. The encoder is either a RNN-cell (torch.nn.GRU) or a Token-Embedding from flair
        if a embedding is handed to the constructor (token_embedding).
        The output of the encoder is used as the initial hidden state to the decoder, which is a RNN-cell (torch.nn.GRU) that predicts
        the lemma of the given token one letter at a time.
        Note that one can use data in which only those words are annotated that differ from their lemma or data in which
        all words are annotated with a (maybe equal) lemma.
        :param embeddings: Embedding used to encode sentence
        :param rnn_input_size: Input size of the RNN('s). Each letter of a token is represented by a hot-one-vector over the given character
            dictionary. This vector is transformed to a input_size vector with a linear layer.
        :param rnn_hidden_size: size of the hidden state of the RNN('s).
        :param rnn_layers: Number of stacked RNN cells
        :param beam_size: Number of hypothesis used when decoding the output of the RNN. Only used in prediction.
        :param char_dict: Dictionary of characters the model is able to process. The dictionary must contain <unk> for the handling
            of unknown characters. If None, a standard dictionary will be loaded. One can either hand over a path to a dictionary or
            the dictionary itself.
        :param label_type: Name of the gold labels to use.
        :param max_sequence_length_dependent_on_input: If set to True, the maximum length of a decoded sequence in the prediction
            depends on the sentences you want to lemmatize. To be precise the maximum length is computed as the length of the longest
            token in the sentences plus one.
        :param max_sequence_length: If set to True and max_sequence_length_dependend_on_input is False a fixed maximum length for
            the decoding will be used for all sentences.
        :param use_attention: whether or not to use attention. Only sensible if encoding via RNN
        :param padding_in_front_for_encoder: In batch-wise prediction we fill up inputs to encoder to the size of the maximum length
            token in the respective batch. If  padding_in_front_for_encoder is True we fill up in the front, otherwise in the back of the vectors.
        """

        super().__init__()

        self._label_type = label_type
        self.beam_size = beam_size
        self.max_sequence_length = max_sequence_length
        self.dependent_on_input = max_sequence_length_dependent_on_input
        self.padding_in_front_for_encoder = padding_in_front_for_encoder
        self.start_symbol = start_symbol_for_encoding
        self.end_symbol = end_symbol_for_encoding
        self.batching_in_rnn = batching_in_rnn
        self.bi_encoding = bidirectional_encoding
        self.rnn_hidden_size = rnn_hidden_size

        # whether to encode characters and whether to use attention (attention can only be used if chars are encoded)
        self.encode_characters = encode_characters
        self.use_attention = use_attention
        if not self.encode_characters:
            self.use_attention = False

        # character dictionary for decoding and encoding
        self.char_dictionary = char_dict if isinstance(char_dict, Dictionary) else Dictionary.load(char_dict)

        # make sure <unk> is in dictionary for handling of unknown characters
        if not self.char_dictionary.add_unk:
            raise KeyError("<unk> must be contained in char_dict")

        # add special symbols to dictionary if necessary and save respective indices
        self.dummy_index = self.char_dictionary.add_item('<>')
        self.start_index = self.char_dictionary.add_item('<S>')
        self.end_index = self.char_dictionary.add_item('<E>')

        # ---- ENCODER ----
        # encoder character embeddings
        self.encoder_character_embedding = nn.Embedding(len(self.char_dictionary), rnn_input_size)

        # encoder pre-trained embeddings
        self.encoder_embeddings = embeddings

        hidden_input_size = 0
        if embeddings: hidden_input_size += embeddings.embedding_length
        if encode_characters: hidden_input_size += rnn_hidden_size
        if bidirectional_encoding: hidden_input_size += rnn_hidden_size
        self.emb_to_hidden = nn.Linear(hidden_input_size, rnn_hidden_size)

        # encoder RNN
        self.encoder_rnn = nn.GRU(input_size=rnn_input_size, hidden_size=self.rnn_hidden_size, batch_first=True,
                                  num_layers=rnn_layers, bidirectional=self.bi_encoding)

        # additional encoder linear layer if bidirectional encoding
        if self.bi_encoding:
            self.bi_hidden_states_to_hidden_size = nn.Linear(2 * self.rnn_hidden_size, self.rnn_hidden_size)

        # ---- DECODER ----
        # decoder: linear layers to transform vectors to and from alphabet_size
        self.decoder_character_embedding = nn.Embedding(len(self.char_dictionary), rnn_input_size)

        # when using attention we concatenate attention outcome and decoder hidden states
        self.character_decoder = nn.Linear(2 * self.rnn_hidden_size if self.use_attention else self.rnn_hidden_size,
                                           len(self.char_dictionary))

        # decoder RNN
        self.rnn_input_size = rnn_input_size
        self.rnn_layers = rnn_layers

        self.decoder_rnn = nn.GRU(input_size=rnn_input_size, hidden_size=self.rnn_hidden_size, batch_first=True,
                                  num_layers=rnn_layers)

        # loss and softmax
        self.loss = nn.CrossEntropyLoss()
        self.unreduced_loss = nn.CrossEntropyLoss(reduction='none')  # for prediction
        self.softmax = nn.Softmax(dim=2)

        self.to(flair.device)

    @property
    def label_type(self):
        return self._label_type

    def words_to_char_indices(self, tokens: List[str], end_symbol=True, start_symbol=False, padding_in_front=False,
                              seq_length=None):
        """
        For a given list of strings this function creates index vectors that represent the characters of the strings.
        Each string is represented by sequence_length (maximum string length + entries for special symbold) many indices representing characters
        in self.char_dict.
        One can manually set the vector length with the parameter seq_length, though the vector length is always at least maximum string length in the
        list.
        :param end_symbol: add self.end_index at the end of each representation
        :param start_symbol: add self.start_index in front of of each representation
        :param padding_in_front: whether to fill up with self.dummy_index in front or in back of strings
        """
        # add additional columns for special symbols if necessary
        c = int(end_symbol) + int(start_symbol)

        max_length = max(len(token) for token in tokens) + c
        if not seq_length:
            sequence_length = max_length
        else:
            sequence_length = max(seq_length, max_length)

        # initialize with dummy symbols
        tensor = self.dummy_index * torch.ones(len(tokens), sequence_length, dtype=torch.long).to(flair.device)

        for i in range(len(tokens)):
            dif = sequence_length - (len(tokens[i]) + c)
            shift = 0
            if padding_in_front:
                shift += dif
            if start_symbol:
                tensor[i][0 + shift] = self.start_index
            if end_symbol:
                tensor[i][len(tokens[i]) + int(start_symbol) + shift] = self.end_index
            for index, letter in enumerate(tokens[i]):
                tensor[i][index + int(start_symbol) + shift] = self.char_dictionary.get_idx_for_item(letter)

        return tensor

    def forward_pass(self, sentences: Union[List[Sentence], Sentence]):

        if isinstance(sentences, Sentence):
            sentences = [sentences]

        # encode inputs
        initial_hidden_states, all_encoder_outputs = self.encode(sentences)

        # get labels (we assume each token has a lemma label)
        labels = [token.get_tag(label_type=self._label_type).value for sentence in sentences for token in sentence]

        # get char indices for labels of sentence
        # (batch_size, max_sequence_length) batch_size = #words in sentence,
        # max_sequence_length = length of longest label of sentence + 1
        decoder_input_indices = self.words_to_char_indices(labels, start_symbol=True, end_symbol=False,
                                                           padding_in_front=False)

        # get char embeddings
        # (batch_size,max_sequence_length,input_size), i.e. replaces char indices with vectors of length input_size
        output_vectors, _ = self.decode(decoder_input_indices, initial_hidden_states, all_encoder_outputs)

        return output_vectors, labels

    def decode(self, decoder_input_indices, initial_hidden_states, all_encoder_outputs: Optional):

        # take decoder input and initial hidden and pass through RNN
        input_tensor = self.decoder_character_embedding(decoder_input_indices)
        output, hidden = self.decoder_rnn(input_tensor, initial_hidden_states)

        # if all encoder outputs are provided, use attention
        if self.use_attention:
            attention_coeff = torch.softmax(torch.matmul(all_encoder_outputs, torch.transpose(output, 1, 2)), dim=1)

            # take convex combinations of encoder hidden states as new output using the computed attention coefficients
            attention_output = torch.transpose(
                torch.matmul(torch.transpose(all_encoder_outputs, 1, 2), attention_coeff), 1, 2)

            output = torch.cat((output, attention_output), dim=2)

        # transform output to vectors of size len(char_dict) -> (batch_size, max_sequence_length, alphabet_size)
        output_vectors = self.character_decoder(output)
        return output_vectors, hidden

    def encode(self, sentences):

        # get all tokens
        tokens = [token for sentence in sentences for token in sentence]

        # variable to store initial hidden states for decoder
        initial_hidden_for_decoder = []
        all_encoder_outputs = None

        # encode input characters by sending them through RNN
        if self.encode_characters:
            # get one-hots for characters and add special symbols / padding
            encoder_input_indices = self.words_to_char_indices([token.text for token in tokens],
                                                               start_symbol=self.start_symbol,
                                                               end_symbol=self.end_symbol,
                                                               padding_in_front=self.padding_in_front_for_encoder)

            # determine length of each token
            extra = 0
            if self.start_symbol: extra += 1
            if self.end_symbol: extra += 1
            lengths = torch.tensor([len(token.text) + extra for token in tokens])

            # embed character one-hots
            input_vectors = self.encoder_character_embedding(encoder_input_indices)

            # test packing and padding
            packed_sequence = torch.nn.utils.rnn.pack_padded_sequence(input_vectors,
                                                                      lengths,
                                                                      enforce_sorted=False,
                                                                      batch_first=True,)
            encoding_flat, initial_hidden_states = self.encoder_rnn(packed_sequence)
            all_encoder_outputs, lengths = torch.nn.utils.rnn.pad_packed_sequence(encoding_flat, batch_first=True)

            # since bidirectional rnn is only used in encoding we need to project outputs to hidden_size of decoder
            if self.bi_encoding:

                # initial_hidden_states = torch.cat([initial_hidden_states[0, :, :], initial_hidden_states[1, :, :]],
                #                                   dim=1).unsqueeze(0)

                all_encoder_outputs = self.bi_hidden_states_to_hidden_size(all_encoder_outputs)

                # print(initial_hidden_states.size())
                # concatenate the final hidden states of the encoder. These will be projected to hidden_size of decoder later with self.emb_to_hidden
                # initial_hidden_states = torch.transpose(initial_hidden_states, 0,1).reshape(1,len(tokens),2*self.rnn_hidden_size) # works only for rnn_layers = 1
                conditions = torch.cat(2 * [torch.eye(self.rnn_layers).bool()])
                bi_states = [initial_hidden_states[conditions[:, i], :, :] for i in range(self.rnn_layers)]
                initial_hidden_states = torch.stack([torch.cat((b[0, :, :], b[1, :, :]), dim=1) for b in bi_states])

            initial_hidden_for_decoder.append(initial_hidden_states)

            # mask out vectors that correspond to a dummy symbol (TODO: check attention masking)
            mask = torch.cat((self.rnn_hidden_size * [(encoder_input_indices == self.dummy_index).unsqueeze(2)]), dim=2)
            all_encoder_outputs = torch.where(mask, torch.tensor(0., device=flair.device), all_encoder_outputs)

        # use token embedding as initial hidden state for decoder
        if self.encoder_embeddings:
            # embed sentences
            self.encoder_embeddings.embed(sentences)

            # create initial hidden state tensor for batch (num_layers, batch_size, hidden_size)
            token_embedding_hidden = torch.stack(
                self.rnn_layers * [torch.stack([token.get_embedding() for token in tokens])])
            initial_hidden_for_decoder.append(token_embedding_hidden)

        # concatenate everything together and project to appropriate size for decoder
        initial_hidden_for_decoder = self.emb_to_hidden(torch.cat(initial_hidden_for_decoder, dim=2))

        return initial_hidden_for_decoder, all_encoder_outputs

    def _calculate_loss(self, scores, labels):
        # score vector has to have a certain format for (2d-)loss fct (batch_size, alphabet_size, 1, max_seq_length)
        scores_in_correct_format = scores.permute(0, 2, 1).unsqueeze(2)

        # create target vector (batch_size, max_label_seq_length + 1)
        target = self.words_to_char_indices(labels, start_symbol=False, end_symbol=True, padding_in_front=False)

        target.unsqueeze_(1)  # (batch_size, 1, max_label_seq_length + 1)

        return self.loss(scores_in_correct_format, target)

    def forward_loss(self, sentences: Union[List[Sentence], Sentence]) -> torch.tensor:
        scores, labels = self.forward_pass(sentences)

        return self._calculate_loss(scores, labels)

    def predict(self, sentences: Union[List[Sentence], Sentence],
                label_name='predicted',
                mini_batch_size: int = 16,
                embedding_storage_mode="None",
                return_loss=False,
                print_prediction=False):
        '''
        Predict lemmas of words for a given (list of) sentence(s).
        :param sentences: sentences to predict
        :param label_name: label name used for predicted lemmas
        :param mini_batch_size: number of tokens that are send through the RNN simultaneously, assuming batching_in_rnn is set to True
        :param embedding_storage_mode: default is 'none' which is always best. Only set to 'cpu' or 'gpu' if
            you wish to not only predict, but also keep the generated embeddings in CPU or GPU memory respectively.
        :param return_loss: whether or not to compute and return loss. Setting it to True only makes sense if labels are provided
        :print_prediction: If True, lemmatized sentences will be printed in the console.
        :param batching_in_rnn: If False, no batching will take place in RNN Cell. Tokens are processed one at a time.
        '''
        if self.beam_size == 1:  # batching in RNN only works flawlessly for beam size at least 2
            self.batching_in_rnn = False

        if isinstance(sentences, Sentence):
            sentences = [sentences]

        # filter empty sentences
        sentences = [sentence for sentence in sentences if len(sentence) > 0]
        if len(sentences) == 0:
            return sentences

        # max length of the predicted sequences
        if not self.dependent_on_input:
            max_length = self.max_sequence_length
        else:
            max_length = max([len(token.text) + 1 for sentence in sentences for token in sentence])

        # for printing
        line_to_print = ''

        overall_loss = 0
        number_tokens_in_total = 0

        with torch.no_grad():

            print(self.batching_in_rnn)
            if self.batching_in_rnn:
                dataloader = DataLoader(dataset=SentenceDataset(sentences), batch_size=mini_batch_size)

                for batch in dataloader:

                    # stop if all sentences are empty
                    if not batch: continue

                    # remove previously predicted labels of this type
                    for sentence in batch:
                        for token in sentence:
                            token.remove_labels(label_name)

                    # create list of tokens in batch
                    tokens_in_batch = [token for sentence in batch for token in sentence]
                    number_tokens = len(tokens_in_batch)
                    number_tokens_in_total += number_tokens

                    # encode inputs
                    hidden, all_encoder_outputs = self.encode(batch)

                    # decoding
                    # create input for first pass (batch_size, 1, input_size), first letter is special character <S>
                    # sequence length is always set to one in prediction
                    input_indices = self.start_index * torch.ones(number_tokens, dtype=torch.long,
                                                                  device=flair.device).unsqueeze(1)

                    output_vectors, hidden = self.decode(input_indices, hidden, all_encoder_outputs)

                    out_probs = self.softmax(output_vectors).squeeze(1)
                    # make sure no dummy symbol <> or start symbol <S> is predicted
                    out_probs[:, self.dummy_index] = -1
                    out_probs[:, self.start_index] = -1
                    # pick top beam size many outputs with highest probabilities
                    probabilities, leading_indices = out_probs.topk(self.beam_size, 1)  # max prob along dimension 1
                    # leading_indices and probabilities have size (batch_size, beam_size)

                    if return_loss:
                        # get labels
                        labels = [token.get_tag(label_type=self._label_type).value if token.get_tag(
                            label_type=self._label_type).value else token.text for token in tokens_in_batch]

                        # target vector represents the labels with vectors of indices for characters
                        target = self.words_to_char_indices(labels, start_symbol=False, end_symbol=True,
                                                            padding_in_front=False, seq_length=max_length)

                        losses = self.unreduced_loss(output_vectors.squeeze(1), target[:, 0])
                        losses = torch.stack(self.beam_size * [losses], dim=1).view(-1, 1)
                        # losses are now in the form (beam_size*batch_size,1)
                        # first beam_size many entries belong to first token of the batch, entries from beam_size + 1 until beam_size+beam_size belong to second token, and so on

                    # keep scores of beam_size many hypothesis for each token in the batch
                    scores = torch.log(probabilities).view(-1, 1)  # (beam_size*batch_size,1)

                    # stack all leading indices of all hypothesis and corresponding hidden states in two tensors
                    leading_indices = leading_indices.view(-1, 1)  # this vector goes through RNN in each iteration
                    hidden_states_beam = torch.stack(self.beam_size * [hidden], dim=2).view(self.rnn_layers, -1,
                                                                                            self.rnn_hidden_size)

                    # save sequences so far
                    sequences = torch.tensor([[i.item()] for i in leading_indices], device=flair.device)

                    # keep track of how many hypothesis were completed for each token
                    n_completed = [0 for _ in range(number_tokens)]  # cpu
                    final_candidates = [[] for _ in range(number_tokens)]  # cpu

                    # if all_encoder_outputs returned, expand them to beam size (otherwise keep this as None)
                    batched_encoding_output = torch.stack(self.beam_size * [all_encoder_outputs], dim=1).view(
                        self.beam_size * number_tokens, -1, self.rnn_hidden_size) if self.use_attention else None

                    for j in range(1, max_length):

                        output_vectors, hidden_states_beam = self.decode(leading_indices,
                                                                         hidden_states_beam,
                                                                         batched_encoding_output)

                        # decode with softmax
                        out_probs = self.softmax(output_vectors)
                        # out_probs have size (beam_size*batch_size, 1, alphabet_size)
                        # make sure no dummy symbol <> or start symbol <S> is predicted
                        out_probs[:, 0, self.dummy_index] = -1
                        out_probs[:, 0, self.start_index] = -1
                        # choose beam_size many indices with highest probabilities
                        probabilities, index_candidates = out_probs.topk(self.beam_size, 2)
                        probabilities.squeeze_(1)
                        index_candidates.squeeze_(1)
                        log_probabilities = torch.log(probabilities)
                        # index_candidates have size (beam_size*batch_size, beam_size)

                        # check if an end symbol <E> has been predicted and, in that case, set hypothesis aside
                        for tuple in (index_candidates == self.end_index).nonzero(as_tuple=False):
                            # index of token in in list tokens_in_batch
                            token_number = torch.div(tuple[0], self.beam_size, rounding_mode='trunc')
                            seq = sequences[tuple[0], :]  # hypothesis sequence
                            # hypothesis score
                            score = (scores[tuple[0]] + log_probabilities[tuple[0], tuple[1]]) / (len(seq) + 1)
                            loss = 0
                            if return_loss:
                                o = output_vectors[tuple[0], :, :]
                                t = target[token_number, j].unsqueeze(0)
                                # average loss of output_vectors of sequence
                                loss = (losses[tuple[0], 0] + self.loss(o, t)) / (len(seq) + 1)

                            final_candidates[token_number].append((seq, score, loss))
                            # TODO: remove token if number of completed hypothesis exceeds given value
                            n_completed[token_number] += 1

                            # set score of corresponding entry to -inf so it will not be expanded
                            log_probabilities[tuple[0], tuple[1]] = -inf

                        # get leading_indices for next expansion
                        # find highest scoring hypothesis among beam_size*beam_size possible ones for each token

                        # take beam_size many copies of scores vector and add scores of possible new extensions
                        # size (beam_size*batch_size, beam_size)
                        hypothesis_scores = torch.cat(self.beam_size * [scores], dim=1) + log_probabilities

                        # reshape to vector of size (batch_size, beam_size*beam_size), each row contains beam_size*beam_size scores of the new possible hypothesis
                        hypothesis_scores_per_token = hypothesis_scores.view(number_tokens, self.beam_size ** 2)

                        # choose beam_size best for each token - size (batch_size, beam_size)
                        best_scores, indices_per_token = hypothesis_scores_per_token.topk(self.beam_size, 1)

                        # out of indices_per_token we now need to recompute the original indices of the hypothesis in a list of length beam_size*batch_size
                        # where the first three inidices belong to the first token, the next three to the second token, and so on
                        beam_numbers = []
                        seq_numbers = []

                        for i, row in enumerate(indices_per_token):
                            for l, index in enumerate(row):
                                beam = i * self.beam_size + torch.div(index, self.beam_size, rounding_mode='trunc')
                                seq_number = index % self.beam_size

                                beam_numbers.append(beam.item())
                                seq_numbers.append(seq_number.item())

                        # with these indices we can compute the tensors for the next iteration
                        # expand sequences with corresponding index
                        sequences = torch.cat(
                            (sequences[beam_numbers], index_candidates[beam_numbers, seq_numbers].unsqueeze(1)), dim=1)

                        # add log-probabilities to the scores
                        scores = scores[beam_numbers] + log_probabilities[beam_numbers, seq_numbers].unsqueeze(1)

                        # save new leading indices
                        leading_indices = index_candidates[beam_numbers, seq_numbers].unsqueeze(1)

                        # save corresponding hidden states
                        hidden_states_beam = hidden_states_beam[:, beam_numbers, :]

                        if return_loss:
                            # compute and update losses
                            losses = losses[beam_numbers] + self.unreduced_loss(output_vectors[beam_numbers, 0, :],
                                                                                torch.stack(
                                                                                    self.beam_size * [target[:, j]],
                                                                                    dim=1).view(-1)).unsqueeze(1)

                    # it may happen that no end symbol <E> is predicted for a token in all of the max_length iterations
                    # in that case we append one of the final seuqences without end symbol to the final_candidates
                    best_scores, indices = scores.view(number_tokens, -1).topk(1, 1)

                    for j, (score, index) in enumerate(zip(best_scores.squeeze(1), indices.squeeze(1))):
                        if len(final_candidates[j]) == 0:
                            beam = j * self.beam_size + index.item()
                            loss = 0
                            if return_loss:
                                loss = losses[beam, 0] / max_length
                            final_candidates[j].append((sequences[beam, :], score / max_length, loss))

                    # get best final hypothesis for each token
                    output_sequences = []
                    for l in final_candidates:
                        l_ordered = sorted(l, key=lambda tup: tup[1], reverse=True)
                        output_sequences.append(l_ordered[0])

                    # get characters from index sequences and add predicted label to token
                    for i, seq in enumerate(output_sequences):
                        overall_loss += seq[2]
                        predicted_lemma = ''
                        for idx in seq[0]:
                            predicted_lemma += self.char_dictionary.get_item_for_index(idx)
                        line_to_print += predicted_lemma
                        line_to_print += ' '
                        tokens_in_batch[i].add_tag(tag_type=label_name, tag_value=predicted_lemma)

                    store_embeddings(batch, storage_mode=embedding_storage_mode)

            else:  # no batching in RNN
                # still: embed sentences batch-wise
                dataloader = DataLoader(dataset=SentenceDataset(sentences), batch_size=mini_batch_size)

                for batch in dataloader:

                    # encode = self.encode(batch)

                    if self.encoder_embeddings:
                        # embed sentence
                        self.encoder_embeddings.embed(batch)

                    # no batches in RNN, prediction for each token
                    for sentence in batch:
                        for token in sentence:

                            number_tokens_in_total += 1
                            # remove previously predicted labels of this type
                            token.remove_labels(label_name)

                            if self.encoder_embeddings:
                                hidden_state = self.emb_to_hidden(torch.stack(
                                    self.rnn_layers * [token.get_embedding()])).unsqueeze(1)  # size (1, 1, hidden_size)
                            else:  # encode input using encoder RNN

                                # note that we do not need to fill up with dummy symbols since we process each token seperately
                                input_indices = self.words_to_char_indices([token.text],
                                                                           start_symbol=self.start_symbol,
                                                                           end_symbol=self.end_symbol)

                                input_vectors = self.encoder_character_embedding(
                                    input_indices)  # TODO: encode input in reverse?? Maybe as parameter?

                                all_encoder_outputs, hidden_state = self.encoder_rnn(input_vectors)

                            # input (batch_size, 1, input_size), first letter is special character <S>
                            input_tensor = self.decoder_character_embedding(
                                torch.tensor([self.start_index], device=flair.device)).unsqueeze(1)

                            # first pass
                            output, hidden_state = self.decoder_rnn(input_tensor, hidden_state)

                            if self.use_attention:
                                attention_coefficients = torch.softmax(
                                    torch.matmul(all_encoder_outputs, torch.transpose(output, 1, 2)), dim=1)

                                # take convex combinations of encoder hidden states as new output using the computed attention coefficients
                                attention_output = torch.transpose(
                                    torch.matmul(torch.transpose(all_encoder_outputs, 1, 2), attention_coefficients), 1,
                                    2)

                                output = torch.cat((output, attention_output), dim=2)

                            output_vectors = self.character_decoder(output)
                            out_probs = self.softmax(output_vectors).squeeze(1)
                            # make sure no dummy symbol <> or start symbol <S> is predicted
                            out_probs[0, self.dummy_index] = -1
                            out_probs[0, self.start_index] = -1
                            # take beam size many predictions with highest probabilities
                            probabilities, leading_indices = out_probs.topk(self.beam_size,
                                                                            1)  # max prob along dimension 1
                            log_probabilities = torch.log(probabilities)
                            # leading_indices have size (1, beam_size)

                            loss = 0
                            # get target and compute loss
                            if return_loss:
                                label = token.get_tag(label_type=self._label_type).value if token.get_tag(
                                    label_type=self._label_type).value else token.text

                                target = self.words_to_char_indices([label], start_symbol=False, end_symbol=True,
                                                                    padding_in_front=False, seq_length=max_length)

                                loss = self.loss(output_vectors.squeeze(0), target[:, 0])

                            # the list sequences will contain beam_size many hypothesis at each point of the prediction
                            sequences = []
                            # create one candidate hypothesis for each prediction
                            for j in range(self.beam_size):
                                # each candidate is a tuple consisting of the predictions so far, the last hidden state, the score/log probability and the loss
                                prediction_index = leading_indices[0][j].item()
                                prediction_log_probability = log_probabilities[0][j]

                                candidate = [[prediction_index], hidden_state, prediction_log_probability, loss]
                                sequences.append(candidate)

                            # variables needed for further beam search
                            n_completed = 0
                            final_candidates = []

                            # Beam search after the first run
                            for i in range(1, max_length):
                                new_sequences = []

                                # expand each candidate hypothesis in sequences
                                for seq, hid, score, seq_loss in sequences:
                                    # create input vector
                                    input_index = torch.tensor([seq[-1]], device=flair.device)
                                    input_tensor = self.decoder_character_embedding(input_index).unsqueeze(1)

                                    # forward pass
                                    output, hidden_state = self.decoder_rnn(input_tensor, hid)

                                    if self.use_attention:
                                        attention_coefficients = torch.softmax(
                                            torch.matmul(all_encoder_outputs, torch.transpose(output, 1, 2)), dim=1)

                                        # take convex combinations of encoder hidden states as new output using the computed attention coefficients
                                        attention_output = torch.transpose(
                                            torch.matmul(torch.transpose(all_encoder_outputs, 1, 2),
                                                         attention_coefficients), 1, 2)

                                        output = torch.cat((output, attention_output), dim=2)

                                    output_vectors = self.character_decoder(output)
                                    out_probs = self.softmax(output_vectors).squeeze(1)
                                    # make sure no dummy symbol <> or start symbol <S> is predicted
                                    out_probs[0, self.dummy_index] = -1
                                    out_probs[0, self.start_index] = -1

                                    new_loss = 0
                                    if return_loss:
                                        new_loss = seq_loss + self.loss(output_vectors.squeeze(0), target[:, i])

                                    # get top beam_size predictions
                                    probabilities, leading_indices = out_probs.topk(self.beam_size,
                                                                                    1)  # max prob along dimension 1
                                    log_probabilities = torch.log(probabilities)

                                    # go through each of the top beam_size predictions
                                    for j in range(self.beam_size):

                                        prediction_index = leading_indices[0][j].item()
                                        prediction_log_probability = log_probabilities[0][j].item()

                                        # add their log probability to previous score
                                        s = score + prediction_log_probability

                                        # if this prediction is a STOP symbol, set it aside
                                        if prediction_index == self.end_index:
                                            candidate = [seq, s / (len(seq) + 1), new_loss / (len(seq) + 1)]
                                            final_candidates.append(candidate)
                                            n_completed += 1
                                        # else, create a new candidate hypothesis with updated score and prediction sequence
                                        else:
                                            candidate = [seq + [prediction_index], hidden_state, s, new_loss]
                                            new_sequences.append(candidate)

                                if len(new_sequences) == 0:  # only possible if self.beam_size is 1 and a <E> was predicted
                                    break

                                # order final candidates by score (in descending order)
                                seq_sorted = sorted(new_sequences, key=lambda tup: tup[2], reverse=True)

                                # only use top beam_size hypothesis as starting point for next iteration
                                sequences = seq_sorted[:self.beam_size]

                            # take one of the beam_size many sequences without predicted <E> symbol, if no end symbol was predicted
                            if len(final_candidates) == 0:
                                seq_without_end = sequences[0]
                                candidate = [seq_without_end[0], seq_without_end[2] / max_length,
                                             seq_without_end[3] / max_length]
                                final_candidates.append(candidate)

                            # order final candidates by score (in descending order)
                            ordered = sorted(final_candidates, key=lambda tup: tup[1], reverse=True)
                            best_sequence = ordered[0]

                            overall_loss += best_sequence[2]

                            # get lemma from indices and add label to token
                            predicted_lemma = ''
                            for idx in best_sequence[0]:
                                predicted_lemma += self.char_dictionary.get_item_for_index(idx)
                            line_to_print += predicted_lemma
                            line_to_print += ' '
                            token.add_tag(tag_type=label_name, tag_value=predicted_lemma)

                        store_embeddings(sentence, storage_mode=embedding_storage_mode)

            if print_prediction:
                print(line_to_print)

            if return_loss:
                return overall_loss, number_tokens_in_total

    def _get_state_dict(self):
        model_state = {
            "state_dict": self.state_dict(),
            "embeddings": self.encoder_embeddings,
            "rnn_input_size": self.rnn_input_size,
            "rnn_hidden_size": self.rnn_hidden_size,
            "rnn_layers": self.rnn_layers,
            "char_dict": self.char_dictionary,
            "label_type": self._label_type,
            "beam_size": self.beam_size,
            "max_sequence_length": self.max_sequence_length,
            "dependent_on_input": self.dependent_on_input,
            "use_attention": self.use_attention,
            "padding_in_front_for_encoder": self.padding_in_front_for_encoder,
            "encode_characters": self.encode_characters,
            "start_symbol": self.start_symbol,
            "end_symbol": self.end_symbol,
            "batching_in_rnn": self.batching_in_rnn,
            "bidirectional_encoding": self.bi_encoding
        }

        return model_state

    def _init_model_with_state_dict(state):
        model = Lemmatizer(
            embeddings=state["embeddings"],
            encode_characters=state["encode_characters"],
            rnn_input_size=state["rnn_input_size"],
            rnn_hidden_size=state["rnn_hidden_size"],
            rnn_layers=state["rnn_layers"],
            char_dict=state["char_dict"],
            label_type=state["label_type"],
            beam_size=state["beam_size"],
            max_sequence_length_dependent_on_input=state["dependent_on_input"],
            max_sequence_length=state["max_sequence_length"],
            use_attention=state["use_attention"],
            padding_in_front_for_encoder=state["padding_in_front_for_encoder"],
            start_symbol_for_encoding=state["start_symbol"],
            end_symbol_for_encoding=state["end_symbol"],
            batching_in_rnn=state["batching_in_rnn"],
            bidirectional_encoding=state["bidirectional_encoding"]
        )
        model.load_state_dict(state["state_dict"])
        return model

    def _print_predictions(self, batch, gold_label_type):
        lines = []
        for sentence in batch:
            eval_line = f" - Text:       {' '.join([token.text for token in sentence])}\n" \
                        f" - Gold-Lemma: {' '.join([token.get_tag(gold_label_type).value for token in sentence])}\n" \
                        f" - Predicted:  {' '.join([token.get_tag('predicted').value for token in sentence])}\n\n"
            lines.append(eval_line)
        return lines

    def create_char_dict_from_corpus(corpus: Corpus) -> Dictionary:
        char_dict = Dictionary(add_unk=True)

        char_dict.add_item('<>')  # index 1
        char_dict.add_item('<S>')  # index 2
        char_dict.add_item('<E>')  # index 3

        for sen in corpus.get_all_sentences():
            for token in sen:
                for character in token.text:
                    char_dict.add_item(character)

        return char_dict

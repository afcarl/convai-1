# should contain wrapper classes

import cPickle
import logging
import numpy as np
import re

import lasagne
import theano
from dual_encoder.model import Model as DE_Model
from hredqa.hred_pytorch import HRED_QA

import hred.search as search
import utils
from hred.dialog_encdec import DialogEncoderDecoder
from hred.state import prototype_state
from candidate import CandidateQuestions
import json
import random
import requests
from nltk import sent_tokenize

#logger = logging.getLogger(__name__)
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s.%(funcName)s +%(lineno)s: %(levelname)-8s [%(process)d] %(message)s',
)

NQG_ENDURL = 'http://localhost:8080'


class Model_Wrapper(object):
    """
    Super class for all Model wrappers
    """

    def __init__(self, model_prefix, name):
        """
        Default constructor
        :param model_prefix: path to the model files
        :param name: model name
        """
        self.model_prefix = model_prefix
        self.name = name
        self.speaker_token = ['<first_speaker>', '<second_speaker>']
        if self.name == 'hred-reddit':
            self.speaker_token = ['<speaker_1>', '<speaker_2']

    def _format_to_model(self, text, context_length):
        text = utils.tokenize_utterance(text)
        text = '%s %s </s>' % (
            self.speaker_token[context_length % 2], text.strip().lower())
        return text

    def _format_to_user(self, text):
        text = utils.detokenize_utterance(text)
        # strip, split, join to remove extra spaces
        return ' '.join(text.strip().split())

    def get_response(self, user_id='', text='', context=None, article='', **kwargs):
        """
        Generate a new response, and add it to the context
        :param article:
        :param user_id: id of the person we chat with
        :param text: the new utterance we just received
        :type text: str
        :param context: queue of conversation history: sliding window of most recent utterances
        :type context: array
        :type kwargs extra params
        :return: the generated response as well as the new context
        """
        pass  # TO BE IMPLEMENTED IN SUB-CLASSES

    def preprocess(self, user_id='', article=None, **kwargs):
        """
        Preprocess before model responses, needed for some models (NQG)
        """
        pass  # OPTIONAL, may or may not implement this


class HRED_Wrapper(Model_Wrapper):

    def __init__(self, model_prefix, dict_file, name):
        # Load the HRED model.
        super(HRED_Wrapper, self).__init__(model_prefix, name)
        state_path = '%s_state.pkl' % model_prefix
        model_path = '%s_model.npz' % model_prefix

        state = prototype_state()
        with open(state_path, 'r') as handle:
            state.update(cPickle.load(handle))
        state['dictionary'] = dict_file
        logging.info('Building %s model...' % name)
        self.model = DialogEncoderDecoder(state)
        logging.info('Building sampler...')
        self.sampler = search.BeamSampler(self.model)
        logging.info('Loading model...')
        self.model.load(model_path)
        logging.info('Model built (%s).' % name)

    # must contain this method for the bot
    def get_response(self, user_id='', text='', context=None, article='', **kwargs):
        logging.info('--------------------------------')
        logging.info('Generating HRED response for user %s.' % user_id)
        text = self._format_to_model(text, len(context))
        context.append(text)
        logging.info('Using context: %s' % ' '.join(list(context)))

        samples, costs = self.sampler.sample(
            [' '.join(list(context))],
            ignore_unk=True,
            verbose=False,
            return_words=True
        )
        response = samples[0][0].replace('@@ ', '').replace('@@', '')
        # remove all tags to avoid having <unk>
        response = self._format_to_user(response)
        # add appropriate tags to the response in the context
        context.append(self._format_to_model(response, len(context)))
        logging.info('Response: %s' % response)
        return response, context


class Dual_Encoder_Wrapper(Model_Wrapper):

    def __init__(self, model_prefix, data_fname, dict_fname, name, n_resp=10000):
        super(Dual_Encoder_Wrapper, self).__init__(model_prefix, name)

        try:
            with open('%s_model.pkl' % model_prefix, 'rb') as handle:
                self.model = cPickle.load(handle)
        except Exception as e:
            logging.error("%s\n ERROR: couldn't load the model" % e)
            logging.info("Will create a new one with pretrained parameters")
            # Loading old arguments
            with open('%s_args.pkl' % model_prefix, 'rb') as handle:
                old_args = cPickle.load(handle)

            logging.info("Loading data...")
            with open('%s' % data_fname, 'rb') as handle:
                train_data, val_data, test_data = cPickle.load(handle)
            data = {'train': train_data, 'val': val_data, 'test': test_data}
            # W is the word embedding matrix and word2idx, idx2word are dictionaries
            with open('%s' % dict_fname, 'rb') as handle:
                word2idx, idx2word = cPickle.load(handle)
            W = np.zeros(shape=(len(word2idx), old_args.emb_size))
            for idx in idx2word:
                W[idx] = np.random.uniform(-0.25, 0.25, old_args.emb_size)
            logging.info("W.shape: %s" % (W.shape,))

            logging.info("Creating model...")
            self.model = self._create_model(
                data, W, word2idx, idx2word, old_args)

            logging.info("Set the learned weights...")
            with open('%s_best_weights.pkl' % model_prefix, 'rb') as handle:
                params = cPickle.load(handle)
                lasagne.layers.set_all_param_values(self.model.l_out, params)
            with open('%s_best_M.pkl' % model_prefix, 'rb') as handle:
                M = cPickle.load(handle)
                self.model.M.set_value(M)
            with open('%s_best_embed.pkl' % model_prefix, 'rb') as handle:
                em = cPickle.load(handle)
                self.model.embeddings.set_value(em)

        with open('%s_timings.pkl' % model_prefix, 'rb') as handle:
            timings = cPickle.load(handle)
            # load last timings (when no improvement was done)
            self.model.timings = timings
        logging.info("Model loaded.")

        with open("%s_r-encs.pkl" % model_prefix, 'rb') as handle:
            self.cached_retrieved_data = cPickle.load(handle)
        self.n_resp = n_resp

    def _create_model(self, data, w, word2idx, idx2word, args):
        return DE_Model(
            data=data,
            W=w.astype(theano.config.floatX),
            word2idx=word2idx,
            idx2word=idx2word,
            save_path=args.save_path,
            save_prefix=args.save_prefix,
            max_seqlen=args.max_seqlen,  # default 160
            batch_size=args.batch_size,  # default 256
            # Network architecture:
            encoder=args.encoder,  # default RNN
            hidden_size=args.hidden_size,  # default 200
            n_recurrent_layers=args.n_recurrent_layers,  # default 1
            is_bidirectional=args.is_bidirectional,  # default False
            dropout_out=args.dropout_out,  # default 0.
            dropout_in=args.dropout_in,  # default 0.
            # Learning parameters:
            patience=args.patience,  # default 10
            optimizer=args.optimizer,  # default ADAM
            lr=args.lr,  # default 0.001
            lr_decay=args.lr_decay,  # default 0.95
            fine_tune_W=args.fine_tune_W,  # default False
            fine_tune_M=args.fine_tune_M,  # default False
            # NTN parameters:
            use_ntn=args.use_ntn,  # default False
            k=args.k,  # default 4
            # Regularization parameters:
            penalize_emb_norm=args.penalize_emb_norm,  # default False
            penalize_emb_drift=args.penalize_emb_drift,  # default False
            emb_penalty=args.emb_penalty,  # default 0.001
            penalize_activations=args.penalize_activations,  # default False
            act_penalty=args.act_penalty  # default 500
        )

    def get_response(self, user_id='', text='', context=None, article='', **kwargs):
        logging.info('--------------------------------')
        logging.info('Generating DE response for user %s.' % user_id)
        text = self._format_to_model(text, len(context))
        context.append(text)
        logging.info('Using context: %s' % ' '.join(list(context)))

        # TODO: use tf-idf as a pre-filtering step to only retrive from `self.n_resp`
        # for now, sample `self.n_resp` randomly without replacement
        response_set_idx = range(len(self.cached_retrieved_data['r']))
        np.random.shuffle(response_set_idx)
        response_set_idx = response_set_idx[:self.n_resp]
        response_set_str = [self.cached_retrieved_data['r'][i]
                            for i in response_set_idx]
        response_set_embs = [self.cached_retrieved_data['r_embs'][i]
                             for i in response_set_idx]

        cached_retrieved_data = self.model.retrieve(
            context_set=[' '.join(list(context))],
            response_set=response_set_str,
            response_embs=response_set_embs,
            k=1, batch_size=1, verbose=False
        )
        response = cached_retrieved_data['r_retrieved'][0][0].replace(
            '@@ ', '').replace('@@', '')

        # remove all tags to avoid having <unk>
        response = self._format_to_user(response)
        # add appropriate tags to the response in the context
        context.append(self._format_to_model(response, len(context)))
        logging.info('Response: %s' % response)
        return response, context


class HREDQA_Wrapper(Model_Wrapper):
    def __init__(self, model_prefix, dict_fname, name):
        super(HREDQA_Wrapper, self).__init__(model_prefix, name)

        self.model = HRED_QA(
            dictionary=dict_fname,
            encoder_file='{}encoder_5.model'.format(model_prefix),
            decoder_file='{}decoder_5.model'.format(model_prefix),
            context_file='{}context_5.model'.format(model_prefix)
        )

    def _get_sentences(self, context):
        sents = [re.sub('<[^>]+>', '', p) for p in context]
        return sents

    def _format_to_user(self, text):
        text = super(HREDQA_Wrapper, self)._format_to_user(text)
        if not text.endswith('?'):
            text = text + ' ?'
        # strip, split, join to remove extra spaces
        return ' '.join(text.strip().split())

    def get_response(self, user_id='', text='', context=None, article='', **kwargs):
        logging.info('------------------------------------')
        logging.info('Generating Followup question for user %s.' % user_id)
        text = self._format_to_model(text, len(context))
        context.append(text)
        logging.info('Using context: %s' % ' '.join(list(context)))

        response = self.model.evaluate(
            self.model.encoder_model,
            self.model.decoder_model,
            self.model.context_model,
            self._get_sentences(context)
        )
        response = ' '.join(response)
        response = self._format_to_user(response)
        context.append(self._format_to_model(response, len(context)))
        return response, context


class CandidateQuestions_Wrapper(Model_Wrapper):
    def __init__(self, model_prefix, dict_fname, name):
        super(CandidateQuestions_Wrapper, self).__init__(model_prefix, name)
        # Use these questions if no suitable questions are found
        # TODO: do not hardcode these, use a dictionary
        self.dict_fname = dict_fname
        self.canned_questions = ["That's a short article, don't you think? Not sure what's it about.",
                                 "Apparently I am too dumb for this article. What's it about?"]
        self.models = {}

    def preprocess(self, chat_id='', article_text='', **kwargs):
        logging.info("Preprocessing CandidateQuestions")
        assert isinstance(article_text, basestring)
        self.models[chat_id] = CandidateQuestions(
            article_text, self.dict_fname)

    def _get_sentences(self, context):
        sents = [re.sub('<[^>]+>', '', p) for p in context]
        return sents

    def _format_to_user(self, text):
        text = super(HREDQA_Wrapper, self)._format_to_user(text)
        if not text.endswith('?'):
            text = text + ' ?'
        # strip, split, join to remove extra spaces
        return ' '.join(text.strip().split())

    def get_response(self, chat_id='', text='', context=None, article='', **kwargs):
        logging.info('------------------------------------')
        logging.info('Generating candidate question for chat %s.' % chat_id)
        text = self._format_to_model(text, len(context))
        logging.info(text)
        context.append(text)

        if chat_id in self.models:
            response = self.models[chat_id].get_response()
            if len(response) < 1:
                # select canned response
                response = random.choice(self.canned_questions)
        else:
            response = 'What is this article about?'  # default
        context.append(self._format_to_model(response, len(context)))
        return response, context


class DumbQuestions_Wrapper(Model_Wrapper):
    def __init__(self, model_prefix, dict_fname, name):
        super(DumbQuestions_Wrapper, self).__init__(model_prefix, name)
        self.data = json.load(open(dict_fname, 'r'))

    # check if user text is match to one of the keys
    def isMatch(self, text):
        for key, value in self.data.iteritems():
            if re.match(key, text, re.IGNORECASE):
                return True
        return False

    # return the key which matches
    def getMatch(self, text):
        for key, value in self.data.iteritems():
            if re.match(key, text, re.IGNORECASE):
                return key
        return False

    def get_response(self, user_id='', text='', context=None, **kwargs):
        logging.info('------------------------------------')
        logging.info('Generating dumb question for user %s.' % user_id)
        ctext = self._format_to_model(text, len(context))
        context.append(ctext)
        if self.isMatch(text):
            key = self.getMatch(text)
            response = random.choice(self.data[key])
        else:
            response = ''
        context.append(self._format_to_model(response, len(context)))
        return response, context


class DRQA_Wrapper(Model_Wrapper):
    def __init__(self, model_prefix, dict_fname, name):
        super(DRQA_Wrapper, self).__init__(model_prefix, name)

    # check if user text is match to one of the keys
    def isMatch(self, text):
        for key, value in self.data.iteritems():
            if re.match(key, text, re.IGNORECASE):
                return True
        return False

    # return the key which matches
    def getMatch(self, text):
        for key, value in self.data.iteritems():
            if re.match(key, text, re.IGNORECASE):
                return key
        return False

    def get_response(self, user_id='', text='', context='', article=None, **kwargs):
        logging.info('------------------------------------')
        logging.info('Generating DRQA answer for user %s.' % user_id)
        ctext = self._format_to_model(text, len(context))
        context.append(ctext)
        response = ''
        try:
            res = requests.post('http://localhost:8888/ask',
                                json={'article': article.text, 'question': text})
            res_data = res.json()
            response = res_data['reply']['text']
        except Exception as e:
            print e
            logging.error(e)
        context.append(self._format_to_model(response, len(context)))
        return response, context


class NQG_Wrapper(Model_Wrapper):
    def __init__(self, model_prefix, dict_fname, name):
        super(NQG_Wrapper, self).__init__(model_prefix, name)
        self.questions = {}

    def preprocess(self, chat_id='', article_text='', **kwargs):
        # extract all sentences from the article
        logging.info('Preprocessing the questions for this article')
        # check condition if we use Spacy
        assert isinstance(article_text, basestring)
        sentences = sent_tokenize(article_text)
        try:
            res = requests.post(NQG_ENDURL, json={'sents': sentences})
            res_data = res.json()
            self.questions[chat_id] = res_data
            for item in self.questions[chat_id]:
                item.update({"used": 0})
            logging.info('Preprocessed article')
            self.questions[chat_id].sort(key=lambda x:  x["score"])
        except Exception as e:
            logging.info('Error in NQG article fetching')
            logging.error(e)

    def get_response(self, user_id='', text='', context=None, article=None, **kwargs):
        logging.info('----------------------------------------')
        logging.info('Generating NQG question for user %s.' % user_id)
        response = ''
        if len(self.questions) > 0:
            response = self.questions[user_id][0]['pred']
            self.questions[user_id][0]['used'] += 1
            self.questions[user_id].sort(key=lambda x: x["used"])

        context.append(self._format_to_model(response, len(context)))
        return response, context

    def clean(self, chat_id):
        del self.questions[chat_id]


class Echo_Wrapper(Model_Wrapper):
    def __init__(self, model_prefix, dict_fname, name):
        super(Echo_Wrapper, self).__init__(model_prefix, name)

    def get_response(self, user_id='', text='', context=None, article=None, **kwargs):
        logging.info('------------------------------------')
        logging.info('Generating Echo response for user %s.' % user_id)
        text = self._format_to_model(text, len(context))
        context.append(text)
        logging.info('Using context: %s' % ' '.join(list(context)))

        response = text
        response = self._format_to_user(response)
        context.append(self._format_to_model(response, len(context)))
        return response, context

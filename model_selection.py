# Script to select model conversation
# Initialize all the models here and then reply with the best answer
# some logic-foo need to be done here.
import config
conf = config.get_config()
from models.wrapper import HRED_Wrapper, Dual_Encoder_Wrapper
import random
import copy

class ModelSelection(object):

    def __init__(self):
        self.hred_model_twitter = None
        self.hred_model_reddit = None

    def initialize_models(self):
        self.hred_model_twitter = HRED_Wrapper(conf.hred['twitter_model_prefix'], conf.hred['twitter_dict_file'], 'hred-twitter')
        self.hred_model_reddit = HRED_Wrapper(conf.hred['reddit_model_prefix'], conf.hred['reddit_dict_file'], 'hred-reddit')
        self.de_model_reddit = Dual_Encoder_Wrapper(conf.de['reddit_model_prefix'], conf.de['reddit_data_file'], conf.de['reddit_dict_file'], 'de-reddit')

    def get_response(self,chat_id,text,context):
        # if text containes /start, dont add it to the context
        if '/start' in text:
            # generate first response or not?
            resp = 'Nice article, what is it about?'
            context.append('<first_speaker>' + resp + '</s>')
            return resp,context
        origin_context = copy.deepcopy(context)
        resp1,cont1 = self.hred_model_twitter.get_response(chat_id,text,origin_context)
        origin_context = copy.deepcopy(context)
        resp2,cont2 = self.hred_model_reddit.get_response(chat_id,text,origin_context)
        # doing a random toss for now
        if random.choice([True,False]):
            return resp1,cont1
        else:
            return resp2,cont2






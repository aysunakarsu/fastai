'Model training for NLP'
from ..torch_core import *
from ..basic_train import *
from ..callbacks import *
from ..data_block import CategoryList
from ..basic_data import *
from ..datasets import *
from ..metrics import accuracy
from ..train import GradientClipping
from .models import *
from .transform import *
from .data import *

__all__ = ['RNNLearner', 'LanguageLearner', 'convert_weights', 'decode_spec_tokens', 'get_language_model', 'language_model_learner', 
           'MultiBatchEncoder', 'get_text_classifier', 'text_classifier_learner']

_model_meta = {AWD_LSTM: {'hid_name':'emb_sz', 'url':URLs.WT103_1,
                          'config_lm':awd_lstm_lm_config, 'split_lm': awd_lstm_lm_split,
                          'config_clas':awd_lstm_clas_config, 'split_clas': awd_lstm_clas_split},
               Transformer: {'hid_name':'d_model', 
                             'config_lm':tfmer_lm_config, 'split_lm': tfmer_lm_split,
                             'config_clas':tfmer_clas_config, 'split_clas': tfmer_clas_split},
               TransformerXL: {'hid_name':'d_model', 
                              'config_lm':tfmerXL_lm_config, 'split_lm': tfmerXL_lm_split,
                              'config_clas':tfmerXL_clas_config, 'split_clas': tfmerXL_clas_split}}

def convert_weights(wgts:Weights, stoi_wgts:Dict[str,int], itos_new:Collection[str]) -> Weights:
    "Convert the model `wgts` to go with a new vocabulary."
    dec_bias, enc_wgts = wgts['1.decoder.bias'], wgts['0.encoder.weight']
    bias_m, wgts_m = dec_bias.mean(0), enc_wgts.mean(0)
    new_w = enc_wgts.new_zeros((len(itos_new),enc_wgts.size(1))).zero_()
    new_b = dec_bias.new_zeros((len(itos_new),)).zero_()
    for i,w in enumerate(itos_new):
        r = stoi_wgts[w] if w in stoi_wgts else -1
        new_w[i] = enc_wgts[r] if r>=0 else wgts_m
        new_b[i] = dec_bias[r] if r>=0 else bias_m
    wgts['0.encoder.weight'] = new_w
    wgts['0.encoder_dp.emb.weight'] = new_w.clone()
    wgts['1.decoder.weight'] = new_w.clone()
    wgts['1.decoder.bias'] = new_b
    return wgts

class RNNLearner(Learner):
    "Basic class for a `Learner` in NLP."
    def __init__(self, data:DataBunch, model:nn.Module, split_func:OptSplitFunc=None, clip:float=None,
                 alpha:float=2., beta:float=1., metrics=None, **learn_kwargs):
        super().__init__(data, model, **learn_kwargs)
        self.callbacks.append(RNNTrainer(self, alpha=alpha, beta=beta))
        if clip: self.callback_fns.append(partial(GradientClipping, clip=clip))
        if split_func: self.split(split_func)
        is_class = (hasattr(self.data.train_ds, 'y') and (isinstance(self.data.train_ds.y, CategoryList) or 
                                                          isinstance(self.data.train_ds.y, LMLabelList)))
        self.metrics = ifnone(metrics, ([accuracy] if is_class else []))

    def save_encoder(self, name:str):
        "Save the encoder to `name` inside the model directory."
        encoder = get_model(self.model)[0]
        if hasattr(encoder, 'module'): encoder = encoder.module
        torch.save(encoder.state_dict(), self.path/self.model_dir/f'{name}.pth')

    def load_encoder(self, name:str):
        "Load the encoder `name` from the model directory."
        encoder = get_model(self.model)[0]
        if hasattr(encoder, 'module'): encoder = encoder.module
        encoder.load_state_dict(torch.load(self.path/self.model_dir/f'{name}.pth'))
        self.freeze()

    def load_pretrained(self, wgts_fname:str, itos_fname:str, strict:bool=True):
        "Load a pretrained model and adapts it to the data vocabulary."
        old_itos = pickle.load(open(itos_fname, 'rb'))
        old_stoi = {v:k for k,v in enumerate(old_itos)}
        wgts = torch.load(wgts_fname, map_location=lambda storage, loc: storage)
        if 'model' in wgts: wgts = wgts['model']
        wgts = convert_weights(wgts, old_stoi, self.data.train_ds.vocab.itos)
        self.model.load_state_dict(wgts, strict=strict)

    def get_preds(self, ds_type:DatasetType=DatasetType.Valid, with_loss:bool=False, n_batch:Optional[int]=None, pbar:Optional[PBar]=None,
                  ordered:bool=False) -> List[Tensor]:
        "Return predictions and targets on the valid, train, or test set, depending on `ds_type`."
        self.model.reset()
        preds = super().get_preds(ds_type=ds_type, with_loss=with_loss, n_batch=n_batch, pbar=pbar)
        if ordered and hasattr(self.dl(ds_type), 'sampler'):
            sampler = [i for i in self.dl(ds_type).sampler]
            reverse_sampler = np.argsort(sampler)
            preds[0] = preds[0][reverse_sampler,:] if preds[0].dim() > 1 else preds[0][reverse_sampler]
            preds[1] = preds[1][reverse_sampler,:] if preds[1].dim() > 1 else preds[1][reverse_sampler]
        return(preds)

def _select_hidden(model, idxs):
    model[0].hidden = [(h[0][:,idxs,:],h[1][:,idxs,:]) for h in model[0].hidden]
    model[0].bs = len(idxs)

def decode_spec_tokens(tokens):
    new_toks,rule,arg = [],None,None
    for t in tokens:
        if t in [TK_MAJ, TK_UP, TK_REP, TK_WREP]: rule = t
        elif rule is None: new_toks.append(t)
        elif rule == TK_MAJ: 
            new_toks.append(t[:1].upper() + t[1:].lower())
            rule = None
        elif rule == TK_UP:  
            new_toks.append(t.upper())
            rule = None
        elif arg is None: 
            try:    arg = int(t)
            except: rule = None
        else:
            if rule == TK_REP: new_toks.append(t * arg)
            else:              new_toks += [t] * arg
    return new_toks

class LanguageLearner(RNNLearner):
    "Subclass of RNNLearner for predictions."
    
    def predict(self, text:str, n_words:int=1, no_unk:bool=True, temperature:float=1., min_p:float=None, sep:str=' ',
                decoder=decode_spec_tokens):
        "Return the `n_words` that come after `text`."
        ds = self.data.single_dl.dataset
        self.model.reset()
        xb,yb = self.data.one_item(text)
        new_idx = []
        for _ in progress_bar(range(n_words), leave=False):
            res = self.pred_batch(batch=(xb,yb))[0][-1]
            if len(new_idx) == 0: _select_hidden(self.model, [0])
            if no_unk: res[self.data.vocab.stoi[UNK]] = 0.
            if min_p is not None: res[res < min_p] = 0.
            if temperature != 1.: res.pow_(1 / temperature)
            idx = torch.multinomial(res, 1).item()
            new_idx.append(idx)
            xb = xb.new_tensor([idx])[None]
        return text + sep + sep.join(decoder(self.data.vocab.textify(new_idx, sep=None)))
    
    def beam_search(self, text:str, n_words:int, top_k:int=10, beam_sz:int=1000, temperature:float=1., sep:str=' ',
                    decoder=decode_spec_tokens):
        "Return the `n_words` that come after `text` using beam search."
        ds = self.data.single_dl.dataset
        self.model.reset()
        xb, yb = self.data.one_item(text)
        start_idx = xb.clone()
        nodes = None
        scores = xb.new_ones(1).float()
        with torch.no_grad():
            for k in range(n_words):
                out = F.log_softmax(self.model(xb)[0][:,-1], dim=-1)
                values, indices = out.topk(top_k, dim=-1)
                scores = (-values * scores[:,None]).view(-1)
                if nodes is None: 
                    nodes = indices[0][:,None]
                    _select_hidden(self.model, [0] * nodes.size(0))
                else:
                    indices_idx = torch.arange(0,nodes.size(0))[:,None].expand(nodes.size(0), top_k).contiguous().view(-1)
                    sort_idx = scores.argsort()[:beam_sz]
                    scores = scores[sort_idx]
                    nodes = torch.cat([nodes[:,None].expand(nodes.size(0),top_k,nodes.size(1)),
                                       indices[:,:,None].expand(nodes.size(0),top_k,1),], dim=2)
                    nodes = nodes.view(-1, nodes.size(2))[sort_idx]
                    _select_hidden(self.model, indices_idx[sort_idx])
                xb = nodes[:,-1][:,None]
        if temperature != 1.: scores.div_(temperature)
        node_idx = torch.multinomial(1-scores, 1).item()
        return text + sep + sep.join(decoder(self.data.vocab.textify([i.item() for i in nodes[node_idx]], sep=None)))

    def show_results(self, ds_type=DatasetType.Valid, rows:int=5, max_len:int=20):
        from IPython.display import display, HTML
        "Show `rows` result of predictions on `ds_type` dataset."
        ds = self.dl(ds_type).dataset
        x,y = self.data.one_batch(ds_type, detach=False, denorm=False)
        preds = self.pred_batch(batch=(x,y))
        y = y.view(*x.size())
        z = preds.view(*x.size(),-1).argmax(dim=2)
        xs = [ds.x.reconstruct(grab_idx(x, i)) for i in range(rows)]
        ys = [ds.x.reconstruct(grab_idx(y, i)) for i in range(rows)]
        zs = [ds.x.reconstruct(grab_idx(z, i)) for i in range(rows)]

        items = [['text', 'target', 'pred']]
        for i, (x,y,z) in enumerate(zip(xs,ys,zs)):
            txt_x = ' '.join(x.text.split(' ')[:max_len])
            txt_y = ' '.join(y.text.split(' ')[max_len:2*max_len])
            txt_z = ' '.join(z.text.split(' ')[max_len:2*max_len])
            items.append([str(txt_x), str(txt_y), str(txt_z)])
        display(HTML(text2html_table(items, ([34,33,33]))))

def get_language_model(arch:Callable, vocab_sz:int, config:dict=None, drop_mult:float=1.):
    "Create a language model from `arch` and its `config`, maybe `pretrained`."
    meta = _model_meta[arch]
    config = ifnone(config, meta['config_lm'].copy())
    for k in config.keys(): 
        if k.endswith('_p'): config[k] *= drop_mult
    tie_weights,output_p,out_bias = map(config.pop, ['tie_weights', 'output_p', 'out_bias'])
    init = config.pop('init') if 'init' in config else None
    encoder = arch(vocab_sz, **config)
    enc = encoder.encoder if tie_weights else None
    decoder = LinearDecoder(vocab_sz, config[meta['hid_name']], output_p, tie_encoder=enc, bias=out_bias)
    model = SequentialRNN(encoder, decoder)
    return model if init is None else model.apply(init)

def language_model_learner(data:DataBunch, arch, config:dict=None, drop_mult:float=1., pretrained:bool=True,
                           **learn_kwargs) -> 'LanguageLearner':
    "Create a `Learner` with a language model from `data` and `arch`."
    model = get_language_model(arch, len(data.vocab.itos), config=config, drop_mult=drop_mult)
    meta = _model_meta[arch]
    learn = LanguageLearner(data, model, split_func=meta['split_lm'], **learn_kwargs)
    if pretrained:
        if 'url' not in meta: 
            warn("There are no pretrained weights for that architecture yet!")
            return learn
        model_path = untar_data(meta['url'], data=False)
        fnames = [list(model_path.glob(f'*.{ext}'))[0] for ext in ['pth', 'pkl']]
        learn.load_pretrained(*fnames)
        learn.freeze()
    return learn

class MultiBatchEncoder(nn.Module):
    "Create an encoder over `module` that can process a full sentence."
    def __init__(self, bptt:int, max_len:int, module:nn.Module):
        super().__init__()
        self.max_len,self.bptt,self.module = max_len,bptt,module

    def concat(self, arrs:Collection[Tensor])->Tensor:
        "Concatenate the `arrs` along the batch dimension."
        return [torch.cat([l[si] for l in arrs], dim=1) for si in range_of(arrs[0])]
    
    def reset(self): 
        if hasattr(self.module, 'reset'): self.module.reset()

    def forward(self, input:LongTensor)->Tuple[Tensor,Tensor]:
        bs,sl = input.size()
        self.reset()
        raw_outputs, outputs = [],[]
        for i in range(0, sl, self.bptt):
            r, o = self.module(input[:,i: min(i+self.bptt, sl)])
            if i>(sl-self.max_len):
                raw_outputs.append(r)
                outputs.append(o)
        return self.concat(raw_outputs), self.concat(outputs)
    
def get_text_classifier(arch:Callable, vocab_sz:int, n_class:int, bptt:int=70, max_len:int=20*70, config:dict=None, 
                        drop_mult:float=1., lin_ftrs:Collection[int]=None, ps:Collection[float]=None) -> nn.Module:
    "Create a text classifier from `arch` and its `config`, maybe `pretrained`."
    meta = _model_meta[arch]
    config = ifnone(config, meta['config_clas'].copy())
    for k in config.keys(): 
        if k.endswith('_p'): config[k] *= drop_mult
    if lin_ftrs is None: lin_ftrs = [50]
    if ps is None:  ps = [0.1]
    layers = [config[meta['hid_name']] * 3] + lin_ftrs + [n_class]
    ps = [config.pop('output_p')] + ps
    init = config.pop('init') if 'init' in config else None
    encoder = MultiBatchEncoder(bptt, max_len, arch(vocab_sz, **config))
    model = SequentialRNN(encoder, PoolingLinearClassifier(layers, ps))
    return model if init is None else model.apply(init)

def text_classifier_learner(data:DataBunch, arch:Callable, bptt:int=70, max_len:int=70*20, config:dict=None, 
                            pretrained:bool=True, drop_mult:float=1., lin_ftrs:Collection[int]=None, 
                            ps:Collection[float]=None, **learn_kwargs) -> 'TextClassifierLearner':
    "Create a `Learner` with a text classifier from `data` and `arch`."
    model = get_text_classifier(arch, len(data.vocab.itos), data.c, bptt=bptt, max_len=max_len,
                                config=config, drop_mult=drop_mult, lin_ftrs=lin_ftrs, ps=ps)
    meta = _model_meta[arch]
    learn = RNNLearner(data, model, split_func=meta['split_clas'], **learn_kwargs)
    if pretrained:
        if 'url' not in meta: 
            warn("There are no pretrained weights for that architecture yet!")
            return learn
        model_path = untar_data(meta['url'], data=False)
        fnames = [list(model_path.glob(f'*.{ext}'))[0] for ext in ['pth', 'pkl']]
        learn.load_pretrained(*fnames, strict=False)
        learn.freeze()
    return learn
from traceback import print_exc
from queue import Queue
from threading import Thread
import time, types, os, re, unicodedata
import gobject
import json
import functools
from myutils.wrapper import threader
from myutils.config import globalconfig, translatorsetting, dynamicapiname
from myutils.utils import stringfyerror, autosql, PriorityQueue
from myutils.commonbase import ArgsEmptyExc, commonbase
from language import Languages


def furigana_debug_enabled():
    return os.environ.get("LUNA_FURIGANA_DEBUG") == "1"


def furigana_debug_preview(value, limit=240):
    if value is None:
        return None
    value = str(value).replace("\r", "\\r").replace("\n", "\\n")
    if len(value) > limit:
        value = value[:limit] + "..."
    return value


def furigana_debug(stage, **kwargs):
    if not furigana_debug_enabled():
        return
    parts = []
    for key, value in kwargs.items():
        if isinstance(value, str):
            value = furigana_debug_preview(value)
        parts.append(f"{key}={value!r}")
    print("[FURIGANA][{}] {}".format(stage, " ".join(parts)), flush=True)


class Interrupted(Exception):
    pass


class GptDictItem:
    def __init__(self, d: dict = None):
        d = d if d else {}
        self.src = d.get("src")
        self.dst = d.get("dst")
        self.info = d.get("info")


class GptDict:
    def __bool__(self):
        return bool(self.__)

    def __iter__(self):
        for _ in self.L:
            yield _

    def __init__(self, d: "list[dict[str, str]]" = None):
        self.L = [GptDictItem(_) for _ in d] if d else []
        self.__ = d

    def __str__(self):
        return json.dumps(self.__, ensure_ascii=False)


class GptTextWithDict:
    def __init__(
        self,
        rawtext: str = None,
        parsedtext: str = None,
        dictionary=None,
        furigana: str = None,
    ):
        if rawtext and not parsedtext:
            parsedtext = rawtext
        self.parsedtext = parsedtext
        self.dictionary = GptDict(dictionary)
        self.rawtext = rawtext
        self.furigana = furigana.strip() if furigana else ""
        self.retry_untranslated_once = False
        self.retry_reason = ""

    def __str__(self):
        return json.dumps(
            {
                "text": self.parsedtext,
                "gpt_dict": str(self.dictionary),
                "contentraw": self.rawtext,
                "furigana": self.furigana,
            },
            ensure_ascii=False,
        )


class Threadwithresult(Thread):
    def __init__(self, func):
        super(Threadwithresult, self).__init__(daemon=True)
        self.func = func
        self.isInterrupted = True
        self.exception = None

    def run(self):
        try:
            self.result = self.func()
        except Exception as e:
            self.exception = e
        self.isInterrupted = False

    def get_result(self, checktutukufunction=None):
        # Thread.join(self,timeout)
        # 不再超时等待，只检查是否是最后一个请求，若是则无限等待，否则立即放弃。
        while checktutukufunction and checktutukufunction() and self.isInterrupted:
            self.join(0.1)

        if self.isInterrupted:
            raise Interrupted()
        elif self.exception:
            raise self.exception
        else:
            return self.result


def timeoutfunction(func, checktutukufunction=None):
    t = Threadwithresult(func)
    t.start()
    return t.get_result(checktutukufunction)


class basetrans(commonbase):
    def langmap(self):
        # The mapping between standard language code and API language code, if not declared, defaults to using standard language code.
        # But the exception is cht. If api support cht, if must be explicitly declared the support of cht, otherwise it will translate to chs and then convert to cht.
        return {}

    def init(self):
        pass

    def translate(self, content: "str|GptTextWithDict"):
        return ""

    ############################################################
    _globalconfig_key = "fanyi"
    _setting_dict = translatorsetting

    def __init__(self, typename):
        super().__init__(typename)
        if (self.transtype == "offline") and (not self.is_gpt_like):
            self.gconfig["useproxy"] = False
        self.queue = PriorityQueue()
        self.sqlqueue = None
        self.sqlwrite2 = None
        try:
            self._private_init()
        except Exception as e:
            gobject.base.displayinfomessage(
                dynamicapiname(self.typename)
                + " init translator failed : "
                + str(stringfyerror(e)),
                "<msg_error_Translator>",
            )
            print_exc()

        self.lastrequesttime = 0
        self._cache = {}

        self.newline = None

        if not self.never_use_trans_cache:
            try:

                self.sqlwrite2 = autosql(
                    gobject.gettranslationrecorddir(
                        "cache/{}.sqlite".format(self.typename)
                    ),
                    check_same_thread=False,
                    isolation_level=None,
                )
                try:
                    self.sqlwrite2.execute(
                        "CREATE TABLE cache(srclang,tgtlang,source,trans);"
                    )
                except:
                    pass
            except:
                print_exc
            self.sqlqueue = Queue()
            threader(self._sqlitethread)()
        threader(self._fythread)()

    def notifyqueuforend(self):
        if self.sqlqueue:
            self.sqlqueue.put(None)
        self.queue.put(None, 999)

    def _private_init(self):
        self.initok = False
        self.init()
        self.initok = True

    def _sqlitethread(self):
        while self.using:
            task = self.sqlqueue.get()
            if task is None:
                break
            try:
                src, trans = task
                self.sqlwrite2.execute(
                    "DELETE from cache WHERE (srclang=? and tgtlang=? and source=?)",
                    (str(self.srclang_1), str(self.tgtlang_1), src),
                )
                self.sqlwrite2.execute(
                    "INSERT into cache VALUES(?,?,?,?)",
                    (str(self.srclang_1), str(self.tgtlang_1), src, trans),
                )
            except:
                print_exc()

    @property
    def using_gpt_dict(self):
        # 决定translator接口传入GptTextWithDict还是str
        return self.gconfig.get("is_gpt_like", False)

    @property
    def never_use_trans_cache(self):
        return self.transtype in ("pre", "other")

    @property
    def use_trans_cache(self):
        return (self.gconfig.get("use_trans_cache", True)) and (
            not self.never_use_trans_cache
        )

    @property
    def is_gpt_like(self):
        return self.gconfig.get("is_gpt_like", False)

    @property
    def onlymanual(self):
        # Only used during manual translation, not used during automatic translation
        return self.gconfig.get("manual", False)

    @property
    def using(self):
        return self.gconfig["use"]

    @property
    def transtype(self):
        # free/dev/api/offline/pre
        # dev/offline 无视请求间隔
        # pre全都有额外的处理，不走该pipeline，不使用翻译缓存
        # offline不被新的请求打断
        return self.gconfig.get("type", "free")

    def gettask(self, content):
        # fmt: off
        callback, contentsolved, callback, is_auto_run, optimization_params = content
        # fmt: on
        if callback:
            priority = 1
        else:
            priority = 0
        self.queue.put(content, priority)

    def longtermcacheget(self, src):
        if not self.sqlwrite2:
            return
        try:
            ret = self.sqlwrite2.execute(
                "SELECT trans FROM cache WHERE (( (srclang=? and tgtlang=?) or  (srclang=? and tgtlang=?)) and source=?)",
                (
                    str(self.srclang_1),
                    str(self.tgtlang_1),
                    str(self.srclang),
                    str(self.tgtlang),
                    src,
                ),
            ).fetchone()
            if ret:
                return ret[0]
            return None
        except:
            print_exc()
            return None

    def longtermcacheset(self, src, tgt):
        if self.sqlqueue:
            self.sqlqueue.put((src, tgt))

    def shorttermcacheget(self, src):
        langkey = (self.srclang_1, self.tgtlang_1)
        if langkey not in self._cache:
            self._cache[langkey] = {}
        try:
            return self._cache[langkey][src]
        except KeyError:
            return None

    def shorttermcacheset(self, src, tgt):
        langkey = (self.srclang_1, self.tgtlang_1)

        if langkey not in self._cache:
            self._cache[langkey] = {}
        self._cache[langkey][src] = tgt

    def shortorlongcacheget(self, content, is_auto_run):
        if self.is_gpt_like and not is_auto_run:
            return None
        if not self.use_trans_cache:
            return
        res = self.shorttermcacheget(content)
        if res:
            return res
        res = self.longtermcacheget(content)
        if res:
            return res
        return None

    def __cap_trans(self, t):
        if isinstance(t, GptTextWithDict) and (
            "_compatible_flag_is_sakura_less_than_5_52_3" in dir(self)
        ):
            t = str(t)
        return self.translate(t)

    def intervaledtranslate(self, content):
        interval = globalconfig["requestinterval"]
        current = time.time()
        self.current = current
        sleeptime = interval - (current - self.lastrequesttime)

        if sleeptime > 0:
            time.sleep(sleeptime)
        self.lastrequesttime = time.time()
        if (current != self.current) or (self.using == False):
            raise Exception()

        return self.multiapikeywrapper(self.__cap_trans)(content)

    def _gptlike_createquery(self, query, usekey, tempk):
        return self._gptlike_get_user_prompt(usekey, tempk).replace("{sentence}", query)

    def _gptlike_get_user_prompt(self, usekey, tempk):
        user_prompt = (
            self.config.get(tempk, "") if self.config.get(usekey, False) else ""
        )
        default = "{DictWithPrompt[When translating, please ensure to translate the specified nouns into the translations I have designated: ]}\n{sentence}"
        user_prompt = user_prompt if user_prompt else default
        if "{sentence}" not in user_prompt:
            user_prompt += "{sentence}"
        return user_prompt

    def _gptlike_createsys(self, usekey, tempk):

        default = "You are a translator. Please help me translate the following {srclang} text into {tgtlang}. You should only tell me the translation result without any additional explanations."
        template = self.config[tempk] if self.config[usekey] else None
        template = template if template else default
        template = self.smartparselangprompt(template)
        return template

    def _gptlike_create_prefill(self, usekey, tempk):
        user_prompt = (
            self.config.get(tempk, "") if self.config.get(usekey, False) else ""
        )
        return user_prompt

    def _gpt_common_parse_context(
        self, messages: list, context: "list[dict]", num: int
    ):
        offset = 0
        _i = 0
        msgs = []
        while (_i + offset < (len(context) // 2)) and (_i < num):
            i = len(context) // 2 - _i - offset - 1
            msgs.append(context[i * 2 + 1])
            msgs.append(context[i * 2])
            _i += 1
        messages.extend(reversed(msgs))

    def maybeneedreinit(self):
        if not (self.needreinit or not self.initok):
            return
        self.needreinit = False
        self.renewsesion()
        try:
            self._private_init()
        except Exception as e:
            raise Exception("init translator failed : " + str(stringfyerror(e)))

    def maybezhconvwrapper(self, callback, tgtlang_1):
        def __maybeshow(callback, tgtlang_1, res, is_iter_res):
            if self.needzhconv:
                res = self.checklangzhconv(tgtlang_1, res)
            callback(res, is_iter_res)

        return functools.partial(__maybeshow, callback, tgtlang_1)

    def __normalize_retry_compare_text(self, text):
        text = unicodedata.normalize("NFKC", text or "")
        return "".join(
            ch
            for ch in text
            if (not ch.isspace()) and (unicodedata.category(ch)[0] not in ("P", "S"))
        )

    def __count_japanese_chars(self, text):
        return len(
            re.findall(
                r"[\u3040-\u30ff\u3400-\u4dbf\u4e00-\u9fff\uff66-\uff9f々〆ヵヶ]",
                unicodedata.normalize("NFKC", text or ""),
            )
        )

    def __count_latin_letters(self, text):
        return len(re.findall(r"[A-Za-z]", text or ""))

    def __should_retry_untranslated_english(
        self, tgtlang_1, contentsolved: "GptTextWithDict|str", res
    ):
        if tgtlang_1 != Languages.English:
            return None
        if not self.is_gpt_like:
            return None
        if isinstance(contentsolved, GptTextWithDict) and contentsolved.retry_untranslated_once:
            return None
        if self.srclang_1 not in (Languages.Auto, Languages.Japanese):
            return None
        if not isinstance(res, str) or (not res.strip()):
            return None
        source = (
            contentsolved.rawtext if isinstance(contentsolved, GptTextWithDict) else contentsolved
        )
        if not isinstance(source, str) or (not source.strip()):
            return None
        if self.__count_japanese_chars(source) == 0:
            return None

        normalized_source = self.__normalize_retry_compare_text(source)
        normalized_res = self.__normalize_retry_compare_text(res)
        if normalized_source and (normalized_source == normalized_res):
            return "source_echo"

        jp_count = self.__count_japanese_chars(res)
        if jp_count == 0:
            return None

        latin_count = self.__count_latin_letters(res)
        if (latin_count == 0) and (jp_count >= 2):
            return "mostly_japanese"

        meaningful = jp_count + latin_count
        if (
            meaningful >= 4
            and (jp_count / meaningful) >= 0.6
            and jp_count >= (latin_count + 2)
        ):
            return "mostly_japanese"
        return None

    def __maybe_retry_untranslated_english(
        self, tgtlang_1, contentsolved: "GptTextWithDict|str", res
    ):
        retry_reason = self.__should_retry_untranslated_english(
            tgtlang_1, contentsolved, res
        )
        if not retry_reason:
            return res

        source = (
            contentsolved.rawtext if isinstance(contentsolved, GptTextWithDict) else contentsolved
        )
        furigana_debug(
            "AUTO_RETRY",
            reason=retry_reason,
            rawtext=source,
            response=res,
        )
        try:
            if isinstance(contentsolved, GptTextWithDict):
                contentsolved.retry_untranslated_once = True
                contentsolved.retry_reason = retry_reason
            return self.intervaledtranslate(contentsolved)
        except Exception as e:
            furigana_debug(
                "AUTO_RETRY_FAIL",
                reason=retry_reason,
                error=stringfyerror(e),
            )
            return res

    def translate_and_collect(
        self, tgtlang_1, contentsolved: "GptTextWithDict|str", is_auto_run, callback
    ):
        if isinstance(contentsolved, GptTextWithDict):
            cache_use = contentsolved.rawtext
            if contentsolved.dictionary or contentsolved.furigana:
                cache_use = str(contentsolved)
            TS_use = contentsolved
        else:
            cache_use = TS_use = contentsolved

        res = self.shortorlongcacheget(cache_use, is_auto_run)
        if isinstance(contentsolved, GptTextWithDict):
            furigana_debug(
                "CACHE",
                hit=bool(res),
                rawtext=contentsolved.rawtext,
                furigana=contentsolved.furigana,
                has_dictionary=bool(contentsolved.dictionary),
            )
        if not res:
            res = self.intervaledtranslate(TS_use)
        if not isinstance(res, types.GeneratorType):
            res = self.__maybe_retry_untranslated_english(
                tgtlang_1, TS_use, res
            )
        # 不能因为被打断而放弃后面的操作，发出的请求不会因为不再处理而无效，所以与其浪费不如存下来
        # gettranslationcallback里已经有了是否为当前请求的校验，这里无脑输出就行了

        callback = self.maybezhconvwrapper(callback, tgtlang_1)
        if isinstance(res, types.GeneratorType):
            collectiterres = ""
            for _res in res:
                if _res == "\0":
                    collectiterres = ""
                elif _res:  # 可能为None
                    collectiterres += _res
                callback(collectiterres, 1)
            callback(collectiterres, 2)
            res = collectiterres

        else:
            if globalconfig["fix_translate_rank"]:
                # 这个性能会稍微差一点，不然其实可以全都这样的。
                callback(res, 1)
                callback(res, 2)
            else:
                callback(res, 0)

        # 保存缓存
        # 不管是否使用翻译缓存，都存下来
        self.shorttermcacheset(cache_use, res)
        self.longtermcacheset(cache_use, res)

    def __parse_gpt_dict(self, contentsolved, optimization_params):
        gpt_dict = []
        contentraw = contentsolved
        furigana = ""
        for _ in optimization_params:
            if isinstance(_, dict):
                _gpt_dict = _.get("gpt_dict", None)
                if _gpt_dict:
                    gpt_dict = _gpt_dict
                    contentraw = _.get("gpt_dict_origin")
                if _.get("furigana_text"):
                    furigana = _.get("furigana_text")

        return GptTextWithDict(
            parsedtext=contentsolved,
            dictionary=gpt_dict,
            rawtext=contentraw,
            furigana=furigana,
        )

    def _fythread(self):
        self.needreinit = False
        while self.using:

            content = self.queue.get()
            if not self.using:
                break
            if content is None:
                break
            # fmt: off
            callback, contentsolved, waitforresultcallback, is_auto_run, optimization_params = content
            # fmt: on
            if self.onlymanual and is_auto_run:
                continue
            if self.srclang_1 == self.tgtlang_1:
                callback(None, 0)
                continue
            try:
                checktutukufunction = (
                    lambda: ((waitforresultcallback is not None) or self.queue.empty())
                    and self.using
                )
                if not checktutukufunction():
                    # 检查请求队列是否空，请求队列有新的请求，则放弃当前请求。但对于内嵌翻译请求，不可以放弃。
                    continue

                self.maybeneedreinit()

                if self.using_gpt_dict:
                    contentsolved = self.__parse_gpt_dict(
                        contentsolved, optimization_params
                    )

                func = functools.partial(
                    self.translate_and_collect,
                    self.tgtlang_1,
                    contentsolved,
                    is_auto_run,
                    callback,
                )
                if self.transtype == "offline":
                    # 离线翻译例如sakura不要被中断，因为即使中断了，部署的服务仍然在运行，直到请求结束
                    func()
                else:
                    timeoutfunction(
                        func,
                        checktutukufunction=checktutukufunction,
                    )
            except Exception as e:
                if not (self.using):
                    continue
                if isinstance(e, ArgsEmptyExc):
                    msg = str(e)
                elif isinstance(e, Interrupted):
                    # 因为有新的请求而被打断
                    continue
                else:
                    print_exc()
                    msg = stringfyerror(e)
                    self.needreinit = True
                callback(msg, 0, True)

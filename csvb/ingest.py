import pandas as pd
import numpy as np
import tomlkit as tml
from dataclasses import dataclass
import json
import operator
import datetime
import pathlib
import tomllib

TYPE_MAP = {float: tml.float_,
            int: tml.integer,
            datetime.time: tml.time,
            datetime.datetime: tml.datetime,
            pd.Timestamp: tml.datetime,
            }


def strip_currency(s):
    s = s.replace(",", "").replace("$", "")
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1]
    return s


def sel_factory(rule):

    def func(df):
        bools = []
        for sel in rule.select:
            match sel.op:
                case pd.Series.str.contains | "contains":
                    bools.append(df[sel.column].str.contains(sel.b, na=False))
                case "eq":
                    try:
                        bools.append(operator.eq(df[sel.column], sel.b))
                    except (TypeError, AttributeError, KeyError):
                        bools.append(operator.eq(sel.a, sel.b))
                case _:
                    try:
                        bools.append(sel.op(df[sel.column], sel.b))
                    except (TypeError, AttributeError, KeyError):
                        bools.append(sel.op(sel.a, sel.b))
        bool = bools.pop()
        if len(bools) > 0:
            for b in bools:
                bool = bool & b
        return bool
    return func


def accounts(trans):
    return set(trans["From"]).union(set(trans["To"]))


def read_and_apply(df, rulepth, debug=False):
    with open(rulepth) as f:
        for rule in tml.load(f)["rules"]:
            if debug:
                print(rule)
            apply_rule(Rule(**rule), df)
    return df


def apply_rule(rule, df):
    for apply in rule.apply:

        match apply.op:
            case "assign":
                try:
                    df.loc[sel_factory(rule), apply.column] = apply.b
                except IndexError as e:
                    print(rule)
                    raise e
            case "_":
                raise NotImplementedError


def find_bank_files(bankpath):
    with open(bankpath / "bank_files.toml", 'rb') as f:
        bank = tomllib.load(f)
    fd = {}
    for account in bank["accounts"]:
        fd[account["name"]] = account
        files = []
        # Any in main.
        files = files + list((bankpath).glob(account["file_pattern"]))
        # Any in 202x subdir.
        start = bank["start"]
        while (bankpath / str(start)).exists():
            files = files + \
                list((bankpath / str(start)).glob(account["file_pattern"]))
            start = start + 1
        fd[account["name"]]["files"] = files
    return fd


def ingest_bank_files(files, accountdata, rulespath):
    dfs = []
    for f in files:
        dfs.append(ingest_bank_file(f, accountdata, rulespath))
    return pd.concat(dfs)


def ingest_bank_file(path, accountdata, rulespath):
    kw = {}
    for k in ["parse_dates",
              "names",
              "header",
              "skiprows"]:
        if k in accountdata:
            kw[k] = accountdata[k]

    df = pd.read_csv(path,
                     **kw)

    if "ops" in accountdata:
        for op in accountdata["ops"]:
            if "col2" not in op:
                match op["op"]:
                    case "-":
                        try:
                            df[op["result"]] = -1 * df[op["col1"]]
                        except KeyError:
                            df[op["col1"]] = -1 * df[op["col1"]]
                    case "rename":
                        df[op["result"]] = df[op["col1"]]
                    case "assign":
                        try:
                            df[op["result"]] = df[op["col1"]]
                        except KeyError:
                            df[op["result"]] = op["col1"]
                    case "strip_currency":
                        try:
                            df[op["col1"]] = df[op["col1"]].apply(
                                strip_currency).astype(float)
                        except KeyError:
                            df["Amount"] = df[op["Amount"]].apply(
                                strip_currency).astype(float)
                    case "strip_white_space":
                        df = df.rename(columns=dict(
                            zip(df.columns, [c.strip() for c in df.columns])))
                    case _:
                        o = op["op"]
                        raise NotImplementedError(f"op {o} not implemented")
            else:
                match op["op"]:
                    case "nansum":
                        df[op["result"]] = np.nansum([df[op["col1"]],
                                                      df[op["col2"]]],
                                                     axis=0)
                    case _:
                        o = op["op"]
                        raise NotImplementedError(f"op {o} not implemented")
    try:
        df = read_and_apply(df, rulespath / accountdata["rules_file"])
    except KeyError:
        pass
    except AttributeError as e:
        print(df.dtypes)
        raise e
    return df


def op_to_TOML(op):
    tbl = tml.table()
    for key, item in op.__dict__.items():
        # print(op, key, item)
        # Try to match the type with the toml type.
        try:
            tbl.add(key, TYPE_MAP[type(item)](item))
        except TypeError:
            tbl.add(key, TYPE_MAP[type(item)](str(item)))
        except KeyError:
            try:
                tbl.add(key, item)
            except ValueError:
                tbl.add(key, str(item))

    return tbl


def rule_to_TOML(rule):
    doc = tml.document()

    atab = tml.aot()
    stab = tml.aot()
    for r in rule.apply:
        atab.append(r.to_TOML())

    for r in rule.select:
        stab.append(r.to_TOML())

    doc.add("select", stab)
    doc.add("apply", atab)

    return doc


@ dataclass(init=False)
class SelectOp:
    op: object
    column: str
    a: object
    b: object

    @ classmethod
    def from_json(cls, jsonstr):
        return _op_from_json(cls, jsonstr)

    def __init__(self, op, column=None, a=None, b=None):
        if column is None and a is None:
            raise TypeError("Column or a must be set.")
        try:
            if op.startswith("<built-in function"):
                op = getattr(operator, op[19:-1])
        except (TypeError, AttributeError):
            pass
        self.op = op
        self.column = column
        self.a = a
        self.b = b

    def to_json(self):
        return json.dumps(self, cls=OpEncoder)

    def __add__(self, other):
        return Rule([self, other])


SelectOp.to_TOML = op_to_TOML


@ dataclass
class ApplyOp:
    op: object
    column: str
    b: object = None

    @ classmethod
    def from_json(cls, jsonstr):
        return _op_from_json(cls, jsonstr)

    def __init__(self, op, column, b=None):
        self.op = op
        self.column = column
        self.b = b

    def to_json(self):
        return json.dumps(self, cls=OpEncoder)

    def __add__(self, other):
        return Rule([self, other])

    def __str__(self):
        return self.__repr__()


ApplyOp.to_TOML = op_to_TOML


def _makeOp(kind, d):
    match kind:
        case "select" | "SelectOp":
            op = SelectOp(**d)
        case "apply" | "ApplyOp":
            op = ApplyOp(**d)
        case _:
            raise NotImplementedError(f"No Op of kind: {kind}")
    return op


def _op_from_json(cls, jsn):
    try:
        d = json.loads(jsn)
    except TypeError:
        d = jsn

    return _makeOp(d["type"], d["data"])


def decode_hook(obj):
    try:
        match obj["type"]:
            case "Rule":
                return Rule.from_json(obj)
            case "SelectOp" | "ApplyOp":
                return _makeOp(obj["type"], obj["data"])
            case _:
                return obj.__dict__
    except KeyError:
        pass
    return obj


@ dataclass(init=False)
class Rule:
    select: list[SelectOp]
    apply: list[ApplyOp]

    @ classmethod
    def from_json(cls, jsn):
        try:
            d = json.loads(jsn)
        except TypeError:
            d = jsn

        if d["type"] != "Rule":
            raise TypeError(f"Unable to instantiate {d['type']}"
                            "as {cls.__name__}")
        r = cls()
        r.apply = [ApplyOp.from_json(ap) for ap in d["data"]["apply"]]
        r.select = [SelectOp.from_json(sel) for sel in d["data"]["select"]]
        return r

    def __init__(self, *args, **kwargs):
        self.apply = []
        self.select = []
        for arg in args:
            if isinstance(arg, ApplyOp):
                self.apply.append(arg)
            if isinstance(arg, SelectOp):
                self.select.append(arg)

        for kwarg in kwargs:
            match kwarg:
                case "select" | "apply":
                    if isinstance(kwargs[kwarg], list):
                        for ele in kwargs[kwarg]:
                            self.__add__(_makeOp(kwarg, ele))
                    else:
                        self.__add__(_makeOp(kwarg, kwargs[kwarg]))

    def to_json(self):
        return json.dumps(self, cls=RuleEncoder)

    def __add__(self, other):
        if isinstance(other, ApplyOp):
            self.apply.append(other)
        elif isinstance(other, SelectOp):
            self.select.append(other)
        elif isinstance(other, Rule):
            self.select += other.select
            self.apply += other.apply
        else:
            raise TypeError("Cannot add unknown rule.")


Rule.to_TOML = rule_to_TOML


SOP = SelectOp
AOP = ApplyOp


def to_transactions(tables):
    for account in tables:
        s = pd.Series(
            index=tables[account].index, data=account, name="To")
        try:
            df = pd.concat([df,
                            pd.concat([tables[account][
                                ["Date",
                                 "Description",
                                 "Amount",
                                 "From"]
                            ], s], axis=1)],
                           axis=0)
        except (UnboundLocalError, NameError):
            df = pd.concat([tables[account][
                ["Date",
                 "Description",
                 "Amount",
                 "From"]], s], axis=1).reset_index(drop=True)
    return df.sort_values("Date").reset_index(drop=True)


class RuleEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, Rule):
            d = {"type": obj.__class__.__name__}
            d["data"] = {}
            try:
                d["data"]["select"] = [o.to_json() for o in obj.select]
            # Shouldn't happen but in case obj.select is None
            except TypeError:
                d["data"]["select"] = obj.select

            try:
                d["data"]["apply"] = [o.to_json() for o in obj.apply]
            # Shouldn't happen but in case obj.apply is None
            except TypeError:
                d["data"]["apply"] = obj.apply
            return d
        else:
            return super().default(obj)


class OpEncoder(json.JSONEncoder):
    def default(self, obj):
        if isinstance(obj, (SelectOp, ApplyOp)):
            return {"type": obj.__class__.__name__,
                    "data": obj.__dict__
                    }
        else:
            return super().default(obj)

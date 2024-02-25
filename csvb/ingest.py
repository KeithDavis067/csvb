import pandas as pd
from dataclasses import dataclass
import json


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
                    bools.append(df[sel.column].str.contains(sel.b))
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


def apply_rule(rule, df):
    for apply in rule.apply:

        match apply.op:
            case "assign":
                df.loc[sel_factory(rule), apply.column] = apply.b
            case "_":
                raise NotImplementedError


@ dataclass(init=False)
class SelectOp:
    op: object
    column: str
    a: object
    b: object

    @classmethod
    def from_json(cls, jsonstr):
        return _op_from_json(cls, jsonstr)

    def __init__(self, op, column=None, a=None, b=None):
        if column is None and a is None:
            raise TypeError("Column or a must be set.")
        self.op = op
        self.column = column
        self.a = a
        self.b = b

    def to_json(self):
        return json.dumps(self, cls=OpEncoder)

    def __add__(self, other):
        return Rule([self, other])


@ dataclass
class ApplyOp:
    op: object
    column: str
    b: object = None

    @classmethod
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


def _makeOp(kind, d):
    print(kind, d)
    match kind:
        case "select" | "SelectOp":
            op = SelectOp(**d)
        case "apply" | "ApplyOp":
            op = ApplyOp(**d)
        case _:
            raise NotImplementedError(f"No Op of kind: {kind}")
    return op


def _op_from_json(cls, jsonstr):
    s = json.loads(jsonstr)

    return _makeOp(s["type"], s["data"])


@ dataclass(init=False)
class Rule:
    select: list[SelectOp]
    apply: list[ApplyOp]

    @classmethod
    def from_json(cls, jsonstr):
        d = json.loads(jsonstr)
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

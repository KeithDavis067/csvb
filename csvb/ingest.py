import pandas as pd
from dataclasses import dataclass


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


@dataclass(init=False)
class SelectOp:
    op: object
    column: str
    a: object
    b: object

    def __init__(self, op, column=None, a=None, b=None):
        if column is None and a is None:
            raise TypeError("Column or a must be set.")
        self.op = op
        self.column = column
        self.a = a
        self.b = b

    def __add__(self, other):
        return Rule([self, other])


@dataclass
class ApplyOp:
    op: object
    column: str
    b: object = None

    def __init__(self, op, column, b=None):
        self.op = op
        self.column = column
        self.b = b

    def __add__(self, other):
        return Rule([self, other])

    def __str__(self):
        return self.__repr__()


def _makeOp(kind, d):
    match kind:
        case "select":
            op = SelectOp(**d)
        case "apply":
            op = ApplyOp(**d)
    return op


@dataclass(init=False)
class Rule:
    select: list[SelectOp]
    apply: list[ApplyOp]

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
            index=tables[account].index, data=account, name="From")
        try:
            df = pd.concat([df,
                            pd.concat([bank_tables[account][
                                       ["Date",
                                        "Description",
                                        "Amount",
                                        "To"]
                                       ], s], axis=1)],
                           axis=0)
        except NameError:
            df = pd.concat([tables[account][
                            ["Date",
                             "Description",
                             "Amount",
                             "To"]], s], axis=1).reset_index(drop=True)
    return df

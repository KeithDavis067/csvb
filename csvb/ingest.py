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
                            df["Amount"] = df["Amount"].apply(
                                strip_currency).astype(float)
                    case "strip_whitespace":
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


def gen_init_balance_trans(bal_decl, acct=None):
    bal_decl = bal_decl.sort_values("Date")
    if acct is None:
        df = []
        for acct in set(bal_decl["Account"]):
            df.append(bal_decl[bal_decl["Account"] == acct].iloc[0])
        df = pd.DataFrame(df)
    else:
        df = bal_decl[bal_decl["Account"] == acct].iloc[0]
    df["From"] = "Equity:Initial Balance"
    df["Description"] = "Initial Balance"
    return df.rename({"Statement Balance": "Amount",
                      "Account": "To"}, axis="columns")


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


def to_triple(acct, fill=None):
    if isinstance(acct, str):
        sp = acct.split(":")
    else:
        sp = acct
    if len(sp) == 3:
        return tuple(sp)
    if len(sp) < 3:
        return tuple(sp + (3 - len(sp)) * [fill])
    if len(sp) > 3:
        raise ValueError


def trans_to_ledgers(trans, bal_decl=None):
    """ Return a ledger from a df of transactios.

    """
    fl = trans.groupby("From")
    tl = trans.groupby("To")
    ledgers = {}
    for acct in accounts(trans):
        # From accounts, note the negative applied to Amount.
        try:
            df = pd.DataFrame(trans.loc[
                fl.groups[acct],
                ["Date", "Description", "To"]
            ]).rename(columns={"To": "Transaction Pair"})
            df["Incoming Amount"] = -1 * trans.loc[fl.groups[acct], "Amount"]
            ledger = df
            # If account is not in From group, skip.
        except KeyError:
            pass

        # To accounts.
        try:
            df = pd.DataFrame(trans.loc[
                tl.groups[acct],
                ["Date", "Description", "From"]
            ]).rename(columns={"From": "Transaction Pair"})
            df["Incoming Amount"] = trans.loc[tl.groups[acct], "Amount"]
            try:
                ledger = pd.concat([ledger, df])
            # If account was not also in From accounts list, don't concat.
            except (NameError, UnboundLocalError):
                ledger = df
        # If account is not in list To accounts, skip.
        except KeyError:
            pass

        ledger = apply_balance(ledger,
                               amount_col="Incoming Amount")

        ledger = ledger.sort_values("Date")
        ledger["Account"] = acct
        ledgers[acct] = ledger

    ledgers = pd.concat(list(ledgers.values()), axis=0)
    return ledgers.sort_values("Date").reset_index(drop=True)


def split_ledger_on_date(ledger, date, append_effective_init=True):
    pass


def apply_bal_decl(df, bal_decl, acct=None, date_col=None):
    if date_col is None:
        date_cole = "Date"
    if acct is not None:
        bd_acct = bal_decl[bal_decl["Account"] == acct]
    else:
        bd_acct = bal_decl

    # Create a map of dates to values to insert rows.
    date_to_value = pd.Series(bd_acct["Statement Balance"].values,
                              index=bd_acct.Date).to_dict()
    df["Balance Declaration"] = df['Date'].map(date_to_value)
    df = df.sort_values(date_col)
    return df


def apply_balance(df, amount_col="Amount", date_col="Date"):
    """ Apply an initial balance and return a df with the cumulative balance.

    Parameters:
        df: a Ledger or other df with a date and amount column.
        bal_decl: A dataframe with dates and balances. If balance declaration
            has declarations for multiple accounts, filter before passing or
            pass 'acct' to filter in this method.
        amount_col: The string name of the colum in 'df' to be used for amounts.
        acct: A string with the value to filter the account column in bal_decl.
        date_col: Alternative string value for the column containing dates.
    """

    # Remove all entries before initial balance.
    try:
        df = df.loc[df[df["Description"]
                       == "Initial Balance"].index[0]:]
    except IndexError:
        # If no initial balance declaration, then continue.
        pass
    df = df.sort_values(date_col)
    df["Balance"] = df[amount_col].cumsum()
    return df


def clean_ledger(ledger):
    """Remove transactions occurring before the last transaction with all accounts assigned.
    Intended to catch incomplete data and return a useful ledger set on a per account basis.
    """
    # clean = {}
    # for acct in ledgers:
    try:
        clean = ledger.loc[[ledger["From"] == ""]:, :]
    except KeyError:
        clean = ledger

    return clean


def _last_matched_date(df, date, col="Date", get_row=False):
    if not get_row:
        return df.index[df[col] == pd.Timestamp(date)][-1]
    else:
        return df[df[col] == pd.Timestamp(date)].iloc[-1]


def init_balance(bal_decl, acct, first_date, leq=False):
    # first_date = min(ledger["Date"])
    if leq:
        acct_balances = bal_decl.loc[(bal_decl["Account"] == acct) & (
            bal_decl["Date"] <= first_date)]
    else:
        acct_balances = bal_decl.loc[(bal_decl["Account"] == acct) & (
            bal_decl["Date"] < first_date)]

    # Catch indexerror if no balance found.
    try:
        return acct_balances.sort_values("Date").iloc[-1]
    except IndexError:
        return None


def append_init_row(ledger, acct, bal_decl, leq=False):
    # print(acct)
    # print(ledger)
    # Find proper initial balance.
    init = init_balance(bal_decl, acct, min(ledger["Date"]), leq=leq)

    # Skip if no appropriate balance was found.
    if init is not None:

        # Create df of initial balance row.
        decl_to_ledg_col = {
            "Statement Balance": "Incoming Amount", "Account": "Transaction Pair"}
        init_df = pd.DataFrame(init).T.rename(columns=decl_to_ledg_col)
        init_df["Description"] = "Initial Balance"

        # Add to first row, make sure we are sorted before recalculating balance column.
        ledger = pd.concat([init_df, ledger]).sort_values("Date")
        ledger["Balance"] = ledger["Incoming Amount"].cumsum()
    return ledger


def accts_to_tuples(accounts):
    accts = set()
    for k in accounts:
        s = k.split(":")
        # accts.add(tuple(s + [""] * (3 - len(s))))
        try:
            accts.add(tuple((s[0], "", "")))
            accts.add(tuple((s[0], s[1], "")))
            accts.add(tuple((s[0], s[1], s[2])))
        except IndexError:
            pass
    return sorted(list(accts))


def balances(ledgers):
    pass
# def balance(ledger):
#     """ Return a dataframe of the change in balance."""
#     bals = []
#     firstds = []
#     lastds = []
#     initbals = []
#     rows = []
#
#     for acct in ledgers:
#         rows.append(ledgers[acct]["Date", "Balance

    # for acct in ledgers:
    #     bals.append(ledgers[acct]["Incoming Amount"].sum())
    #
    #     firstds.append(ledgers[acct]["Date"].iloc[0])
    #     lastds.append(ledgers[acct]["Date"].iloc[-1])
    #     initial = ledgers[acct].loc[ledgers[acct]
    #                                 ["Description"] == "Initial Balance"]
    #     if len(initial) != 0:
    #         initbals.append(initial["Amount"])
    #     else:
    #         initbals.append(0)
    #     # initbals.append(ledgers[acct]["Balance"].iloc[0])
    #
    # acct_bals = pd.DataFrame({"Period Start": firstds,
    #                           "Initial Balance": initbals,
    #                           "Period End": lastds,
    #                           "Ending Balance": bals},
    #                          index=ledgers.keys())
    #
    # acct_bals.index.name = "Account String"
    #
    # names = ["Type", "Account", "Subaccount"]
    # acct_bals = acct_bals.sort_index()
    # acct_bals["Difference"] = acct_bals["Ending Balance"] -
    # acct_bals["Initial Balance"]
    return acct_bals

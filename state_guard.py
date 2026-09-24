"""Read-only account checks around a workflow; never repair or seed an account.

The capture file is an untracked job-local checkpoint. Only the owning bot may
write its financial state; verification enforces append-only old trade history.
"""
import argparse
import json
import math
from pathlib import Path


def checked(path):
    p=Path(path)
    if not p.is_file():raise ValueError('required existing account missing: '+str(p))
    value=json.loads(p.read_text(encoding='utf-8'))
    if not isinstance(value,dict) or not value:raise ValueError('invalid account: '+str(p))
    if not isinstance(value.get('trades'),list):raise ValueError('trade history missing: '+str(p))
    def finite(obj):
        if isinstance(obj,float) and not math.isfinite(obj):raise ValueError('non-finite value: '+str(p))
        if isinstance(obj,dict):
            for child in obj.values():finite(child)
        elif isinstance(obj,list):
            for child in obj:finite(child)
    finite(value)
    return value


def verify(old,new):
    for key in ('created_at','initial_cash','initial_balance'):
        if key in old and new.get(key)!=old[key]:raise ValueError('account identity/baseline changed: '+key)
    for key in ('trades','closed_trades'):
        if key in old:
            if not isinstance(new.get(key),list) or new[key][:len(old[key])]!=old[key]:
                raise ValueError('existing history removed or rewritten: '+key)


def main():
    parser=argparse.ArgumentParser()
    parser.add_argument('operation',choices=['capture','verify'])
    parser.add_argument('checkpoint')
    parser.add_argument('paths',nargs='*')
    args=parser.parse_args()
    checkpoint=Path(args.checkpoint)
    if args.operation=='capture':
        if not args.paths:raise ValueError('empty account scope')
        data={path:checked(path) for path in args.paths}
        checkpoint.write_text(json.dumps(data,ensure_ascii=False,allow_nan=False),encoding='utf-8')
    else:
        data=json.loads(checkpoint.read_text(encoding='utf-8'))
        for path,old in data.items():verify(old,checked(path))
    print('Account integrity:',args.operation,'OK;',len(data),'independent accounts')


if __name__=='__main__':main()

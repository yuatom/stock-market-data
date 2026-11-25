"""One mode interpretation for publication routing and the argparse collector."""

def normalize_mode_args(argv):
    values=list(argv);result=[];seen=False;i=0
    while i<len(values):
        token=values[i]
        option=token.split('=',1)[0]
        if option.startswith('--') and len(option)>2 and option!='--mode' and '--mode'.startswith(option):
            raise ValueError('abbreviated --mode is ambiguous; use --mode')
        if token=='--mode' or token.startswith('--mode='):
            if seen:raise ValueError('repeated --mode is ambiguous')
            seen=True
            if token=='--mode':
                i+=1
                if i>=len(values) or values[i].startswith('--'):raise ValueError('--mode requires a value')
                mode=values[i]
            else:mode=token.split('=',1)[1]
            if not mode:raise ValueError('--mode requires a value')
            result.extend(['--mode',mode])
        else:result.append(token)
        i+=1
    return result

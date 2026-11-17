def mask(value,mode,width=None):
    if value is None or mode=='null':return None
    if mode=='raw':return value
    if mode=='bucket' and type(width) is int and width>0:return (value//width)*width
    raise ValueError('invalid mask')

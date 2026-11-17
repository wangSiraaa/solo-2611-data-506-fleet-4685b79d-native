from query_gateway import open_seed,fixed_rows
from policy_catalog import mask
db=open_seed();print([(i,d,mask(s,'bucket',1000)) for i,d,s in fixed_rows(db)])

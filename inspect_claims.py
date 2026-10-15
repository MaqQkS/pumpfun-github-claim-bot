import json,base64,struct,urllib.request,datetime,base58,time
R="https://api.mainnet-beta.solana.com"
def rpc(m,p):
  q=urllib.request.Request(R,json.dumps({"jsonrpc":"2.0","id":1,"method":m,"params":p}).encode(),{"content-type":"application/json"})
  return json.load(urllib.request.urlopen(q))["result"]
d=base64.b64decode(json.load(open('pda_acct.json'))['result']['value']['data'][0])
o=8;bump,ver=d[o],d[o+1];o+=2;n=struct.unpack_from('<I',d,o)[0];o+=4;uid=d[o:o+n].decode();o+=n
plat=d[o];o+=1;tc,lc,tsc=struct.unpack_from('<QQQ',d,o)
print("PDA:",bump,ver,uid,plat,"total_claimed",tc/1e9,"SOL last_claimed",lc,datetime.datetime.fromtimestamp(lc),"stable",tsc)
DISC={bytes([225,21,251,133,161,30,199,226]):"claim_social_fee_pda",bytes([17,77,240,134,58,188,53,149]):"claim_social_fee_pda_v2",bytes([144,224,59,211,78,248,202,220]):"create_social_fee_pda"}
EV=bytes([50,18,193,65,237,210,234,236])
for s in reversed(json.load(open('pda_sigs.json'))['result']):
  t=rpc("getTransaction",[s['signature'],{"encoding":"json","maxSupportedTransactionVersion":0}]);time.sleep(0.4)
  m=t['transaction']['message'];keys=m['accountKeys']+t['meta'].get('loadedAddresses',{}).get('writable',[])+t['meta'].get('loadedAddresses',{}).get('readonly',[])
  names=[]
  allix=[(i,False) for i in m['instructions']]+[(i,True) for g in t['meta'].get('innerInstructions',[]) for i in g['instructions']]
  for ix,inner in allix:
    prog=keys[ix['programIdIndex']]
    if prog.startswith('pfee'):
      raw=base58.b58decode(ix['data'])
      nm=DISC.get(raw[:8], raw[:8].hex())
      if raw[:8]==bytes([228,69,165,46,81,203,154,29]) and raw[8:16]==EV:
        e=raw[16:];o=8;n=struct.unpack_from('<I',e,o)[0];o+=4;u=e[o:o+n].decode();o+=n;pl=e[o];o+=1
        pda=base58.b58encode(e[o:o+32]).decode();o+=32;rec=base58.b58encode(e[o:o+32]).decode();o+=32;auth=base58.b58encode(e[o:o+32]).decode();o+=32
        amt,before,life=struct.unpack_from('<QQQ',e,o)
        nm=f"EVENT SocialFeePdaClaimed user={u} plat={pl} recipient={rec} authority={auth} amount={amt/1e9} lifetime={life/1e9}"
      elif not inner:
        nm+=" accts="+",".join(keys[a][:6] for a in ix['accounts'])
      names.append(nm)
    elif not inner and prog.startswith('6EF8'): names.append("pump:"+base58.b58decode(ix["data"])[:8].hex()+" accts="+",".join(keys[a] for a in ix["accounts"]))
  print(datetime.datetime.fromtimestamp(s['blockTime']),s['signature'][:20],"signer",keys[0][:8],names)

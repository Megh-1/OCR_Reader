import json,re,sys
from collections import Counter
sys.path.insert(0,"/app")
from app.modules import pipeline, field_extraction as fe
from app.modules.preprocessing import preprocess_image
from app.modules.type_identification import identify_document_type
from app.modules.template_store import get_template_version
D="/app/data/demo_50/"
truth=json.load(open(D+"ground_truth.json"))
def toks(s): return re.findall(r"[a-z0-9]+",str(s).lower())
for name in sys.argv[1:]:
    pages,_m,layers=pipeline.prepare_document(open(D+name,"rb").read(),name,None)
    g=preprocess_image(pages[0],deskew=False); ident=identify_document_type(g.image_bytes,None)
    t=get_template_version(ident.template_id,ident.version)
    win={f.id:f.bbox for f in fe._fields_on_page(t,ident.alignment)}
    L=layers[0]; cnt=Counter(tok for w in L.words for tok in toks(w.text))
    print("==",name,ident.template_id)
    for fid,want in truth[name]["fields"].items():
        if fid not in win: continue
        x0,y0,x1,y1=win[fid]
        # the first token of the value that is unique on the page and >=3 chars
        for tok in toks(want)[:1]:
            hit=[w for w in L.words if tok in toks(w.text) and cnt[tok]==1 and len(tok)>=3]
            if hit:
                w=hit[0]; print(f"  {fid:28} y={(y0+y1)/2:.3f} dx(left)={w.bbox[0]-x0:+.4f} dy(centre)={(w.bbox[1]+w.bbox[3])/2-(y0+y1)/2:+.4f}")

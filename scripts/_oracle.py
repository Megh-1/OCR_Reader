import json,sys
import numpy as np
sys.path.insert(0,"/app"); sys.path.insert(0,"/app/scripts")
from app.modules import pipeline, field_extraction as fe, layer_registration as lr
from app.modules.preprocessing import preprocess_image
from app.modules.type_identification import identify_document_type
from app.modules.template_store import get_template_version
from export_batch_json import same
lr.TEXT_LAYER_REG_NEIGHBOURHOOD=0.10
D="/app/data/demo_50/"; truth=json.load(open(D+"ground_truth.json"))
tot=0;ora=0;chosen_right=0
for name in sys.argv[1:]:
    pages,_m,layers=pipeline.prepare_document(open(D+name,"rb").read(),name,None)
    g=preprocess_image(pages[0],deskew=False); ident=identify_document_type(g.image_bytes,None)
    t=get_template_version(ident.template_id,ident.version)
    fields=[f for f in fe._fields_on_page(t,ident.alignment) if f.from_field is None]
    sh=lr.register_boxes(layers[0],fields)
    grid=lr._grid()
    print("==",name)
    for f in fields:
        want=truth[name]["fields"].get(f.id)
        if want is None: continue
        good=[]
        for s in grid:
            ff=f.model_copy(update={"bbox":lr.shifted(f.bbox,tuple(s))})
            if same(f.id,fe.extract_field_from_text_layer(layers[0],ff).raw_text,want): good.append(s)
        tot+=1
        if good:
            ora+=1
            best=min(good,key=lambda s:abs(s).sum())
            c=sh.get(f.id,(0,0))
            ok=any(abs(c[0]-s[0])<1e-6 and abs(c[1]-s[1])<1e-6 for s in good)
            chosen_right+=ok
            print(f"  {f.id:28} oracle=({best[0]:+.4f},{best[1]:+.4f}) n={len(good):3d} chosen=({c[0]:+.4f},{c[1]:+.4f}) {'OK' if ok else 'x'}")
        else: print(f"  {f.id:28} unreachable")
print("fields",tot,"reachable",ora,"chosen shift reads right",chosen_right)

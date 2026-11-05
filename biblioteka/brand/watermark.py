"""Полупрозрачный водяной знак «Миграционный Альянс» внизу картинки.
python3 watermark.py in.png [out.png]"""
import sys
from PIL import Image, ImageDraw, ImageFont
EM='/home/claude/migrant-app/biblioteka/brand/emblemy/emblem_gold_small.png'
FB='/usr/share/fonts/opentype/inter/Inter-Bold.otf'
def wm(src,dst,alpha=0.55):
    im=Image.open(src).convert('RGBA'); W,H=im.size
    u=W/1080
    em=Image.open(EM).convert('RGBA'); s=int(70*u); em.thumbnail((s,s))
    f=ImageFont.truetype(FB,int(34*u)); t='МИГРАЦИОННЫЙ АЛЬЯНС'
    lay=Image.new('RGBA',(W,H),(0,0,0,0)); d=ImageDraw.Draw(lay)
    tw=d.textlength(t,font=f); gap=int(18*u); total=em.width+gap+tw
    x=(W-total)/2; y=H-int(150*u)
    lay.paste(em,(int(x),int(y)),em)
    d.text((x+em.width+gap,y+(em.height-int(34*u))/2-2*u),t,font=f,fill=(255,255,255,255),stroke_width=int(2*u),stroke_fill=(0,0,0,160))
    a=lay.getchannel('A').point(lambda v:int(v*alpha)); lay.putalpha(a)
    out=Image.alpha_composite(im,lay).convert('RGB'); out.save(dst,quality=93)
if __name__=='__main__':
    wm(sys.argv[1], sys.argv[2] if len(sys.argv)>2 else sys.argv[1])

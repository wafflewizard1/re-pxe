"""Bounded ASCII iPXE heading; text fallback works without GOP/graphics."""
from io import BytesIO
import re
from fastapi import HTTPException
from PIL import Image, ImageDraw, ImageFont

SIZES = {'large': (800,600), 'standard': (1024,768), 'compact': (1280,800)}
COLORS = {'teal': ('10252e','35d4b1',6), 'blue': ('101e35','6babff',4), 'amber': ('292114','f0be55',3)}

APPEARANCE_PATH = 'routines-appearance.ipxe'
DEFAULT_APPEARANCE = '#!ipxe\n# re-pxe menu text colours. iPXE documentation: https://ipxe.org/cmd/cpair\n# Palette 6 is the selected theme accent; 7 is white; 0 is the theme background.\n# Background 9 is transparent on graphical consoles and black on text consoles.\n# Indices for colour definitions: 0-7. Pair indices: 0-7. Index 8 is invalid.\n# To give the selected line an accent rectangle, change pair 2 background to 6.\n# To set custom text RGB, add: colour --basic 7 --rgb 0xffffff 7\ncpair --foreground 7 --background 9 0\ncpair --foreground 7 --background 9 1\ncpair --foreground 6 --background 9 2\ncpair --foreground 6 --background 9 3\ncpair --foreground 7 --background 9 4\ncpair --foreground 1 --background 9 5\ncpair --foreground 6 --background 9 6\ncpair --foreground 6 --background 9 7\n'

def appearance_lines(content):
    """The active menu colour file configures appearance, without boot routing."""
    if len(content.encode('utf-8')) > 8192 or not content.replace('\r\n','\n').startswith('#!ipxe\n'):
        raise ValueError('Menu colours require #!ipxe and at most 8 KiB')
    lines=[]
    for line in content.splitlines()[1:]:
        line=line.strip()
        if not line or line.startswith('#'):continue
        tokens=line.split()
        if tokens[-2:]==['||','echo']:tokens=tokens[:-2]
        if not tokens or tokens[0] not in ('colour','cpair'):
            raise ValueError('Menu colours accept only colour and cpair commands; edit labels in routines-menu.json')
        command=tokens[0];allowed={'--basic','--rgb'} if command=='colour' else {'--foreground','--background'}
        options=tokens[1:-1]
        if not re.fullmatch(r'[0-7]',tokens[-1]) or len(options)%2:
            raise ValueError('Colour and pair indices must be 0-7')
        seen=set()
        for key,value in zip(options[::2],options[1::2]):
            if key not in allowed or key in seen:raise ValueError('Invalid or repeated colour option')
            seen.add(key)
            valid=re.fullmatch(r'0x[0-9a-fA-F]{6}',value) if key=='--rgb' else re.fullmatch(r'(?:[0-7]|9|15)' if key=='--basic' else r'(?:[0-7]|9)',value)
            if not valid:raise ValueError('Use a six-digit RGB colour or a supported ANSI/palette index')
        lines.append(' '.join(tokens)+' || echo')
        if len(lines)>64:raise ValueError('Menu colours are limited to 64 commands')
    return lines


def saved_appearance(store):
    from .file_workspace import text_file
    try:return appearance_lines(text_file(store,'configuration',APPEARANCE_PATH)['content'])
    except ValueError as exc:raise HTTPException(503,'Invalid routines-appearance.ipxe; repair menu colours in the text editor: '+str(exc)) from None


def commands(menu,address,custom_background=False):
    width,height=SIZES[menu.text_size];bg,accent,ansi=COLORS[menu.theme]
    lines=[]
    if menu.graphics:
        picture=f' --picture http://{address}:8089/pxe/routines/background.png'
        top=24 if custom_background or not menu.show_banner else 120
        lines.append(f'console --x {width} --y {height} --left 24 --right 24 --top {top} --bottom 24'+picture+' || console || echo Using firmware text console')
    else:
        lines.append('console || echo Using firmware text console')
    # Only palette indices 0-7 can be redefined. Background 9 is the built-in
    # black/transparent default; the old invalid index 8 left the blue UI pair.
    lines += [f'colour --basic 0 --rgb 0x{bg} 0 || echo', f'colour --basic {ansi} --rgb 0x{accent} 6 || echo',
              'colour --basic 7 --rgb 0xe9f3f7 7 || echo', 'cpair --foreground 7 --background 9 0 || echo',
              'cpair --foreground 7 --background 9 1 || echo', 'cpair --foreground 6 --background 9 2 || echo',
              'cpair --foreground 6 --background 9 3 || echo']
    return lines

def background(store,menu):
    width,height=SIZES[menu.text_size];bg,accent,_=COLORS[menu.theme]
    image=Image.new('RGB',(width,height),'#'+bg)
    from .boot_background import read
    from PIL import ImageOps
    picture=read(store)
    if picture:
        with Image.open(BytesIO(picture)) as source:image=ImageOps.fit(source.convert('RGB'),(width,height),method=Image.Resampling.LANCZOS)
        output=BytesIO();image.save(output,format='PNG');return output.getvalue()
    draw=ImageDraw.Draw(image)
    draw.rectangle((24,100,width-24,103),fill='#'+accent)
    if menu.show_banner:
        heading = r""" _ __ ___        _ __ __  _____
| '__/ _ \ _____| '_ \ \/ / _ \
| | |  __/|_____| |_) |>  <  __/
|_|  \___|      | .__/ /_/\_\___|
                |_|"""
        font = ImageFont.truetype('/usr/share/fonts/truetype/dejavu/DejaVuSansMono-Bold.ttf', 17)
        bounds=draw.multiline_textbbox((0,0),heading,font=font,spacing=0)
        draw.multiline_text(((width-(bounds[2]-bounds[0]))//2,7-bounds[1]),heading,font=font,spacing=0,fill='#'+accent)
    output=BytesIO();image.save(output,format='PNG');return output.getvalue()

'use strict';
// Apply browser preferences before CSS paints, including on the sign-in page.
const appearanceKey='repxe.appearance.v1';
function readBrowserValue(key,fallback,session=false){try{const storage=session?sessionStorage:localStorage;return JSON.parse(storage.getItem(key))??fallback;}catch{return fallback;}}
function writeBrowserValue(key,value,session=false){try{(session?sessionStorage:localStorage).setItem(key,JSON.stringify(value));}catch{/* Preferences still work when browser storage is unavailable. */}}
const savedAppearance=readBrowserValue(appearanceKey,{});
let appearance={mode:['system','light','dark'].includes(savedAppearance?.mode)?savedAppearance.mode:'system',theme:['teal','blue','amber'].includes(savedAppearance?.theme)?savedAppearance.theme:'blue',collapsed:savedAppearance?.collapsed===true};
const systemColor=window.matchMedia?.('(prefers-color-scheme: dark)');
function applyAppearance(){document.documentElement.dataset.mode=appearance.mode==='system'?(systemColor?.matches?'dark':'light'):appearance.mode;document.documentElement.dataset.theme=appearance.theme;document.documentElement.dataset.sidebar=appearance.collapsed?'collapsed':'expanded';}
applyAppearance();systemColor?.addEventListener('change',()=>{applyAppearance();if(typeof syncAppearanceControls==='function')syncAppearanceControls();});

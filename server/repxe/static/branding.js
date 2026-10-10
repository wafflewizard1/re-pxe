/* Shared logo applies to sign-in, the portal and newly generated reports. */
async function applyBranding(value){
 const logo=document.getElementById('brand-logo'),icon=document.querySelector('.brand-icon'),preview=document.getElementById('branding-preview');
 logo.hidden=!value.logo_url;preview.hidden=!value.logo_url;icon.hidden=!!value.logo_url;
 if(value.logo_url){logo.src=value.logo_url;preview.src=value.logo_url;}else{logo.removeAttribute('src');preview.removeAttribute('src');}
 document.getElementById('branding-empty').hidden=!!value.logo_url;
 document.getElementById('branding-remove').disabled=!value.logo_url;
 document.getElementById('site-favicon').href=value.favicon_url;
}
fetch('/api/branding',{credentials:'same-origin',cache:'no-store'}).then(r=>r.ok?r.json():Promise.reject(Error('Logo unavailable'))).then(applyBranding).catch(()=>{});
async function saveBranding(method,file){
 if(!webSession)throw Error('Sign in before changing the logo.');
 const response=await fetch('/api/branding/logo',{method,credentials:'same-origin',headers:{'X-CSRF-Token':webSession.csrf_token,...(file?{'Content-Type':file.type||'application/octet-stream'}:{})},...(file?{body:file}:{})});
 const value=await response.json();if(!response.ok)throw Error(value.detail||'Logo could not be saved');
 await applyBranding(value);
}
document.getElementById('branding-form').addEventListener('submit',async event=>{
 event.preventDefault();const button=event.submitter;button.disabled=true;
 try{const file=document.getElementById('branding-file').files[0];if(!file)throw Error('Select a logo image.');if(file.size>5*1024*1024)throw Error('Logo must be at most 5 MiB.');await saveBranding('PUT',file);document.getElementById('branding-file').value='';message('Logo saved for the website, favicon and reports.');}catch(error){message(error.message,true);}finally{button.disabled=false;}
});
document.getElementById('branding-remove').addEventListener('click',async event=>{
 const button=event.currentTarget;button.disabled=true;try{await saveBranding('DELETE');message('Custom logo removed.');}catch(error){button.disabled=false;message(error.message,true);}
});

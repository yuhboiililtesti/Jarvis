/* Display-only device context. The browser cannot attest its own identity.
   Backend must derive true identity from authenticated device credentials. */
const deviceEl=document.getElementById('device-origin');
const targetEl=document.getElementById('execution-target');
const platform=navigator.userAgentData?.platform||navigator.platform||'Unknown device';
const ua=navigator.userAgent||'';
const type=/iPhone/i.test(ua)?'iPhone browser':/iPad/i.test(ua)?'iPad browser':/Windows/i.test(platform)?'Windows browser':/Mac/i.test(platform)?'Mac browser':/Linux/i.test(platform)?'Linux browser':'Web browser';
if(deviceEl){deviceEl.textContent=type+' · unverified';deviceEl.title='Browser-reported platform only. Not an authenticated device ID.';}
if(targetEl){targetEl.textContent='Core policy · APOS';targetEl.title='Display default only; actual execution target is chosen server-side.';}
// Only accept authoritative identity from an authenticated session endpoint if implemented.
// Never fall back to an arbitrary URL query parameter or client-provided host name.
try {
  const res=await fetch('/api/mobile/v1/devices/me',{credentials:'same-origin',cache:'no-store',headers:{Accept:'application/json'}});
  if(res.ok){const data=await res.json();if(data&&typeof data.device_id==='string'&&typeof data.display_name==='string'){
    if(deviceEl){deviceEl.textContent=data.display_name;deviceEl.title='Authenticated device: '+data.device_id;}
  }}
} catch (_) { /* endpoint optional for now */ }

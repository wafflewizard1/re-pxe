"""Shared capability catalogue and policy resolution; no device access."""
import hashlib
import json
from typing import Literal
NWIPE = {
    'zero': 'Fill with zeros', 'one': 'Fill with ones', 'prng': 'PRNG stream',
    'ops2': 'RCMP TSSIT OPS-II', 'dodshort': 'DoD 5220.22-M (short)',
    'dod522022m': 'DoD 5220.22-M (full)', 'gutmann': 'Gutmann',
    'is5enh': 'HMG IS5 Enhanced', 'bruce7': 'Schneier (7 passes)',
    'bmb': 'BMB21-2019', 'verify_zero': 'Verify zeros (read only)',
    'verify_one': 'Verify ones (read only)',
}
PRNG = {'auto': 'Automatic', 'chacha20': 'ChaCha20', 'aes_ctr_prng': 'AES-256-CTR',
        'isaac64': 'ISAAC-64', 'isaac': 'ISAAC', 'mersenne': 'Mersenne Twister',
        'add_lagg_fibonacci_prng': 'Lagged Fibonacci', 'xoroshiro256_prng': 'XORoshiro-256',
        'splitmix64': 'SplitMix64'}
NATIVE = {'sanitize_block': 'nwipe Block Erase', 'sanitize_crypto': 'nwipe Crypto Erase',
          'sanitize_overwrite': 'nwipe Sanitize Overwrite'}
PRESETS = {'nist_clear': 'NIST Clear', 'nist_purge': 'NIST Purge'}
METHODS = {**NWIPE, **NATIVE, **PRESETS}
NATIVE_VERSION = '0.43-repxe1'
Method = Literal[*METHODS]
OPTION_KEYS = ('method', 'verification', 'rounds', 'final_blank', 'prng', 'io_mode',
               'unfreeze', 'readback', 'readback_pattern', 'overwrite_pattern', 'invert_pattern', 'crypto_approved',
               'direction', 'sync_blocks', 'retry_io_errors', 'abort_on_block_errors')
EXTENDED_KEYS = ('execution_method', 'crypto_assurance')


class Unsupported(ValueError):
    """A read-only capability check rejected the entire batch before writes."""


class Uncertain(ValueError):
    """Controller state cannot safely authorize any write or automatic reboot."""


def native(method): return method in NATIVE or method == 'nist_purge'
def extended(method): return method in NATIVE or method in PRESETS


def authorized_keys(policy):
    return OPTION_KEYS + (EXTENDED_KEYS if extended(policy['method']) else ()) + (('prng_followup',) if policy['method']=='nist_purge' else ())


def fingerprint(policy):
    p = settings(policy)
    # Existing software policies retain byte-for-byte credential compatibility.
    return hashlib.sha256(json.dumps({k: p.get(k, False) for k in authorized_keys(p)}, sort_keys=True).encode()).hexdigest()


def catalogue():
    return [
        dict(id=k, name=v, engine='nwipe', verification=['all'] if k.startswith('verify_') or k == 'is5enh' else ['all', 'last', 'off'],
             rounds=not k.startswith('verify_'), final_blank=k not in ('ops2', 'verify_zero', 'verify_one'),
             prng=k not in ('zero', 'one', 'verify_zero', 'verify_one'), unfreeze=False,
             pattern=False, invert=False, crypto=False, readback=False)
        for k, v in NWIPE.items()] + [
        dict(id=k, name=v, engine='nwipe', verification=['all'], rounds=False, final_blank=False,
             prng=False, unfreeze=False, pattern=False, invert=False, crypto=k=='sanitize_crypto', readback=False)
        for k,v in {**NATIVE,**PRESETS}.items()]


def settings(policy, method=None):
    """Normalize controls disabled by the method; never invent verification."""
    p = dict(policy); p['method'] = method or p['method']
    c = next((c for c in catalogue() if c['id'] == p['method']), None)
    if not c: raise ValueError('Only nwipe methods are supported; select nwipe and republish the routine')
    # Legacy stored drafts may contain this field; new inputs reject it.
    if p.get('fallback_method') and p['fallback_method'] not in METHODS: raise ValueError('Only nwipe methods are supported')
    verification = p.get('verification', 'all')
    if verification not in ('all', 'last', 'off', 'controller'): raise ValueError('Unknown verification mode')
    p['verification'] = verification if verification in c['verification'] else c['verification'][0]
    p['rounds'] = p.get('rounds', 1) if c['rounds'] else 1
    if not 1 <= p['rounds'] <= 16: raise ValueError('Rounds must be 1–16')
    p['final_blank'] = bool(p.get('final_blank', True)) if c['final_blank'] else False
    p['prng'] = p.get('prng', 'auto') if c['prng'] else 'auto'
    if p['prng'] not in PRNG: raise ValueError('Unknown random generator')
    p['io_mode'] = p.get('io_mode', 'auto') if c['engine'] == 'nwipe' else 'auto'
    if p['io_mode'] not in ('auto', 'direct', 'cached'): raise ValueError('Unknown I/O mode')
    p['direction'] = p.get('direction', 'forward') if c['engine'] == 'nwipe' else 'forward'
    if p['direction'] not in ('forward', 'reverse', 'scatter'): raise ValueError('Unknown I/O direction')
    p['sync_blocks'] = p.get('sync_blocks') if c['engine'] == 'nwipe' else None
    p['retry_io_errors'] = bool(p.get('retry_io_errors', True)) if c['engine'] == 'nwipe' else True
    p['abort_on_block_errors'] = bool(p.get('abort_on_block_errors', True)) if c['engine'] == 'nwipe' else True
    p['unfreeze'] = bool(p.get('unfreeze', False)) if c['unfreeze'] else False
    p['readback'] = p.get('readback', 'none') if c['readback'] else 'none'
    if p['readback'] not in ('none', 'sample', 'full'): raise ValueError('Unknown readback mode')
    p['readback_pattern'] = p.get('readback_pattern', 'readable') if p['readback'] != 'none' else 'readable'
    if p['readback_pattern'] not in ('readable', 'zero', 'one'): raise ValueError('Unknown readback expectation')
    p['overwrite_pattern'] = p.get('overwrite_pattern', 0) if c['pattern'] else 0
    p['invert_pattern'] = bool(p.get('invert_pattern', False)) if c['invert'] else False
    if not c['crypto']: p['crypto_approved']=False
    if p['method'] in NATIVE or p['method'] in PRESETS:
        p.update(verification='all', rounds=1, final_blank=False, prng='auto', io_mode='auto',
                 direction='forward', sync_blocks=None, retry_io_errors=True, abort_on_block_errors=True)
        execution=p.get('execution_method') or ('zero' if p['method']=='nist_clear' else 'sanitize_block' if p['method']=='nist_purge' else p['method'])
        allowed={'zero'} if p['method']=='nist_clear' else {'sanitize_block','sanitize_overwrite'} if p['method']=='nist_purge' else {p['method']}
        if execution not in allowed: raise ValueError('Execution method does not match the selected nwipe preset')
        p['execution_method']=execution
        if p['method']=='nist_purge':
            followup=p.get('prng_followup',True)
            if type(followup) is not bool:raise ValueError('PRNG follow-up must be a boolean')
            p['prng_followup']=followup
        p['crypto_assurance']=p.get('crypto_assurance') or ''
        p['crypto_approved']=bool(p.get('crypto_approved',False)) if p['method']=='sanitize_crypto' else False
        if p['method']=='sanitize_crypto' and (not p['crypto_approved'] or len(p['crypto_assurance'].strip())<20):
            raise ValueError('Crypto Erase requires recorded assurance for encryption coverage, strength and complete key sanitization')
    return p


def resolve(policy, capabilities, disk=None, batch=None):
    """Resolve only read-only, exact-scope nwipe capabilities; never fall back."""
    p=settings(policy)
    if capabilities.get('engine_version')==NATIVE_VERSION and capabilities.get('sanitize_status')=='uncertain':
        raise Uncertain('Native controller state is uncertain; inspect the PC without erasing or rebooting')
    if not native(p['method']): return p,False
    methods=set(capabilities.get('methods',[]))
    if capabilities.get('engine_version') != NATIVE_VERSION or capabilities.get('sanitize_status')=='uncertain':
        raise Uncertain('Native sanitize capability or controller state is uncertain; inspect the PC without erasing or rebooting')
    if capabilities.get('sanitize_status')=='unsupported':
        raise Unsupported('Drive does not support the requested nwipe sanitize method; use the routine Failure route')
    if capabilities.get('sanitize_status')!='ready': raise Uncertain('Native sanitize probe status is missing; inspect the PC')
    if p['method']=='nist_purge':
        choices=['sanitize_overwrite'] if disk and disk.get('rotational') else ['sanitize_block']
        p['execution_method']=next((m for m in choices if m in methods),'')
    if p['execution_method'] not in methods:
        raise Unsupported('Drive does not support the requested nwipe sanitize method; use the routine Failure route')
    expected={(d['identity'],d['size_bytes']) for d in batch or ([disk] if disk else [])}
    scope={(d['identity'],d['size_bytes']) for d in capabilities.get('scope_targets',[])}
    target=(disk['identity'],disk['size_bytes']) if disk else None
    if capabilities.get('scope')=='unknown' or not scope: raise Uncertain('Native sanitize scope is uncertain; inspect the PC')
    if capabilities.get('scope')=='controller' and not capabilities.get('controller_id'):
        raise Uncertain('Native controller identity is missing; inspect the PC')
    if not target or capabilities.get('scope') not in ('disk','controller') or target not in scope or not scope <= expected:
        raise Unsupported('Native sanitize scope is unknown or includes a drive outside the authorized batch')
    # Current nwipe extension rejects multiple namespaces/subsystem controllers.
    if len(scope)!=1:
        raise Unsupported('Native sanitize currently requires one namespace on one controller')
    return p,False

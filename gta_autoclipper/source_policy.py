"""Conservative episodic-source gate; uploader provenance is NOT a rights license."""
import re
from pathlib import Path

# Exact platform handles, not substring matches on display names or 'official' titles.
PUBLISHER_HANDLES = {
    '@crunchyroll', '@netflixanime', '@museasia', '@anioneasia',
    '@disney', '@disneypixar', '@pixar', '@disneychannel', '@disneyanimation',
    '@cartoonnetwork', '@dreamworks', '@dreamworkstv', '@nickelodeon',
}
EDITED = re.compile(r'\b(amv|gmv|mmv|edits?|edited|compilations?|fancams?|montages?|reactions?|reacts|reupload|repost|mashup|shorts|tiktok|sped[ -]?up|slowed|nightcore)\b|\btop\s*\d+', re.I)
SCENE = re.compile(r'\b(episodes?|scenes?|clips?|fights?|vs|sneak peek)\b', re.I)
FAN = re.compile(r'fan[ -]?made|fan animation|original animation|independent animation', re.I)


def source_decision(candidate, cfg):
    extra = candidate.extra or {}
    # Explicit local-file supply only; never interpret a cached download as local authorization.
    if candidate.source == 'local' and Path(candidate.url).is_file():
        return True, 'local_supplied', 'user_supplied', 'local footage supplied; rights not verified'
    title = candidate.title or ''
    tags = ' '.join(str(x) for x in extra.get('tags', []))
    desc = str(extra.get('description') or '')
    if EDITED.search(title + ' ' + tags) or re.search(r'fan edit|amv|reuploaded|reposted|reaction mashup', desc, re.I):
        return False, 'edited_repost', 'unknown', 'edited/AMV/compilation/reaction source'
    handle = str(extra.get('uploader_id') or '').lower()
    channel_id = str(extra.get('channel_id') or '')
    approved = set((cfg.get('source_policy') or {}).get('authorized_channels') or [])
    trusted = handle in PUBLISHER_HANDLES or handle in {str(x).lower() for x in approved} or (channel_id and channel_id in approved)
    uploader = 'publisher_handle' if handle in PUBLISHER_HANDLES else ('user_authorized' if trusted else 'unverified')
    if not trusted:
        return False, 'unknown', uploader, 'unverified uploader: supply local footage or explicitly authorize its exact channel ID/handle'
    if FAN.search(title):
        if handle not in {str(x).lower() for x in approved} and channel_id not in approved:
            return False, 'original_fan_animation', uploader, 'fan animation needs explicit channel authorization'
        return True, 'original_fan_animation', uploader, 'authorized original fan animation, not an episode'
    if not SCENE.search(title) or re.search(r'\b(trailer|teaser|best moments|highlights)\b', title, re.I):
        return False, 'promotional_or_unknown', uploader, 'require an episode or continuous scene/clip, not trailers/highlights'
    return True, 'episode_scene', uploader, 'publisher/authorized scene metadata; not a copyright clearance'


def enforce_source(candidate, cfg):
    if cfg.get('niche_id') not in {'anime', 'animation'}:
        return True
    ok, kind, uploader, reason = source_decision(candidate, cfg)
    candidate.extra = dict(candidate.extra or {}, source_kind=kind, uploader_status=uploader)
    print(f'[source-policy] kind={kind} uploader={uploader} accepted={ok} reason={reason}', flush=True)
    return ok


def source_queries(niche, fan=False):
    if fan:
        return ['original fan animation creator scene -amv -edit -compilation']
    if niche == 'anime':
        return ['Crunchyroll episode fight scene -amv -edit -compilation -shorts',
                'Muse Asia anime episode scene -edit -amv', 'Ani One Asia episode scene -edit -amv',
                'Netflix Anime official scene clip -compilation -edit -amv']
    return ['Pixar official movie scene clip -anime -edit -compilation',
            'Disney Animation official scene clip -anime -shorts -edit',
            'Cartoon Network episode clip -anime -compilation',
            'DreamWorks official movie scene clip -anime -edit -compilation']

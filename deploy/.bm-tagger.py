#!/usr/bin/env python3
# Tagger bitmagnet : un tag de langue + un tag de type pour chaque torrent,
# deduits du nom (nomenclature release). Usage :
#   python3 bm-tagger.py <ndjson> dry          -> compte la distribution, ne modifie rien
#   python3 bm-tagger.py <ndjson> apply [N]    -> applique a tout (ou aux N premiers)
import json
import re
import sys
import time
import urllib.request
from collections import Counter

GQL = "http://192.168.1.111:3333/graphql"
BATCH = 500

LANG_RE = {
    "multi": re.compile(r"\b(multi|multilanguage|multilingue)\b", re.I),
    # VF/VFF/VFI/VQ/TrueFrench = audio francais ; FRA = code langue FR
    "francais": re.compile(r"\b(french|truefrench|francais|fran\u00e7ais|vf|vff|vfi|vq|fra)\b", re.I),
    # VO/VOST/VOSTFR = version originale (anglais dans les faits) sous-titree
    "anglais": re.compile(r"\b(english|eng|vo|vost|vostfr)\b", re.I),
}
SERIES_RE = re.compile(
    # NB : \bS\d{1,2}\b ne matche PAS a l'interieur de S02E12 (pas de
    # frontiere de mot entre le chiffre et 'E') -> saison nue seulement.
    r"\b(s\d{1,2}\s?e\d{1,3}|\bs\d{1,2}\b|saison\s*\d+|season\s*\d+|episode\s*\d+|\d{1,2}x\d{2})\b",
    re.I,
)
YEAR_RE = re.compile(r"\b(19|20)\d{2}\b")
# Codes langue explicites non FR/EN -> autre-langue (rus, ita, castellano...)
OTHER_LANG_RE = re.compile(
    r"\b(rus|russian|ita|italian|esp|spanish|castellano|ger|german|deu|jap|japanese|kor|chi|"
    r"mandarin|nld|dutch|flemish|pol|por|portuguese|brazilian|swe|nor|dan|fin|ara|hin|tam|tel|"
    r"ind|tha|vie|ukr|ces|cze|hun|ron|ro|tur|ell|greek|heb|far|per|yts|pl|lektor|dubbing|dub)\b",
    re.I,
)
# Contenus non video : jamais movies/series meme avec une annee
NON_MEDIA_RE = re.compile(
    r"\b(ebook|e-book|epub|pdf|mobi|azw3|audiobook|mp3|flac|discography|ost|lossless|"
    r"crack|keygen|setup|installer|iso|apk|exe|win64|win-mac|font|psd|vector|wallpaper|"
    r"xxx|nintendo|switch|ps4|ps5|xbox|emu|firmware|bios|trainer|repack-fitgirl|dodi)\b",
    re.I,
)


def tags_for(name):
    if LANG_RE["multi"].search(name):
        lang = "multi"
    elif LANG_RE["francais"].search(name):
        lang = "francais"
    elif LANG_RE["anglais"].search(name):
        lang = "anglais"
    elif OTHER_LANG_RE.search(name):
        lang = "autre-langue"
    else:
        lang = "lang-inconnue"
    if SERIES_RE.search(name):
        typ = "series"
    elif NON_MEDIA_RE.search(name):
        typ = "autre"
    elif YEAR_RE.search(name):
        typ = "movies"
    else:
        typ = "autre"
    return lang, typ


def gql(query, tries=3):
    # Retry avec backoff : bitmagnet peut avoir un coup de mou pendant le
    # crawl DHT (TimeoutError vecu pendant une mutation de masse).
    last = None
    for attempt in range(tries):
        try:
            req = urllib.request.Request(
                GQL, json.dumps({"query": query}).encode(), {"Content-Type": "application/json"}
            )
            with urllib.request.urlopen(req, timeout=90) as r:
                return json.load(r)
        except Exception as e:
            last = e
            print("gql erreur (%d/%d) : %s" % (attempt + 1, tries, e), file=sys.stderr)
            time.sleep(10 * (attempt + 1))
    raise last


def main():
    items = [json.loads(l) for l in open(sys.argv[1]) if l.strip()]
    mode = sys.argv[2] if len(sys.argv) > 2 else "dry"
    limit = int(sys.argv[3]) if len(sys.argv) > 3 else None
    if limit:
        items = items[:limit]

    dist = Counter(tags_for(it["name"]) for it in items)
    print("== distribution (langue, type) ==", file=sys.stderr)
    for (lang, typ), n in dist.most_common():
        print(f"{n:>8}  {lang:<14} {typ}", file=sys.stderr)

    if mode == "dry":
        return

    # ATTENTION (vecu) : putTags applique CHAQUE tagName a CHAQUE infoHash
    # (produit cartesien). Il faut donc grouper les torrents par PAIRE de
    # tags identique et faire un appel putTags par groupe.
    groups = {}
    for it in items:
        lang, typ = tags_for(it["name"])
        groups.setdefault((lang, typ), []).append(it["h"])

    total = sum(len(v) for v in groups.values())
    done = 0
    skipped = 0
    for (lang, typ), hashes in groups.items():
        for i in range(0, len(hashes), BATCH):
            chunk = hashes[i : i + BATCH]
            res = gql(
                "mutation { torrent { setTags(infoHashes: %s, tagNames: %s) } }"
                % (json.dumps(chunk), json.dumps([lang, typ]))
            )
            if "errors" in res:
                msg = res["errors"][0].get("message", "")
                if "SQLSTATE 23503" in msg or "foreign key" in msg.lower():
                    # hash supprime par bitmagnet entre-temps : rejouer un par un
                    for h in chunk:
                        r1 = gql(
                            "mutation { torrent { setTags(infoHashes: [%s], tagNames: %s) } }"
                            % (json.dumps(h), json.dumps([lang, typ]))
                        )
                        if "errors" in r1:
                            skipped += 1
                        else:
                            done += 1
                    print("chunk FK : rejoue a l'unite (%d skippes cumules)" % skipped, file=sys.stderr)
                    continue
                print("ERREUR batch %s : %s" % ((lang, typ), msg), file=sys.stderr)
                sys.exit(1)
            done += len(chunk)
        print("groupe %s : ok" % ((lang, typ),), file=sys.stderr)
        if done % 50000 < BATCH:
            print("progression : %d / %d" % (done, total), file=sys.stderr)
    print("TERMINE : %d tages, %d ignores (hash disparus) " % (done, skipped), file=sys.stderr)


if __name__ == "__main__":
    main()

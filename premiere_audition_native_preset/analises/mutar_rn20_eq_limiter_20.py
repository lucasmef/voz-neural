#!/usr/bin/env python3
"""Insere EQ parametrico + limiter v1 em 20 clipes RN20+Auto-Match.

Mutador deliberadamente fail-closed: entrada e doador possuem SHA fixo, a
curva vem de JSON estrito, nenhum arquivo e sobrescrito, e somente as 20
AudioComponentChains podem mudar; EQ/limiter e Params sao os unicos objetos
novos. Fontes de audio nao sao abertas nem analisadas.
"""

from __future__ import annotations

import argparse
import base64
import copy
import gzip
import json
import math
import re
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path
from xml.etree import ElementTree as ET

import criar_copia_clean_sem_enhance_20 as safe
from criar_copia_107_c1_dynamics import fmt, set_param_scalar


ROOT = Path(__file__).resolve().parents[2]
NATIVE = ROOT / "premiere_audition_native_preset"
PROJECTS = (NATIVE / "projetos_descartaveis").resolve()
TRIAL = NATIVE / "comparacao_ab/SEM_ENHANCE_ESSENTIAL_20_20260814"
PREFLIGHT = NATIVE / "comparacao_ab/FLUXO_APROVADO_20_20260814/manifest_preflight.json"

INPUT = PROJECTS / "Teste_SEM_ENHANCE_RN20_EQ_WORKING.prproj"
DONOR = PROJECTS / "Teste_107_SHELF60M12_ONLY_REAL_WORKING.prproj"
EXPECTED_INPUT_SHA = "2d9b97f423cdec834dc2423840164d6d31c847490a3518e12afd2fbe07b31e25"
EXPECTED_DONOR_SHA = "86f07a141b73d2140ecac0a4837363fd2461de72b32e781d0f9a321a68c2b994"
EXPECTED_PREFLIGHT_SHA = "9989b1fb65798053621c1e342a95dc6f75a7dcdf134f16ed162745b237316ccd"
EXPECTED_IDS = [64, 65, 66, 67, 68, 69, 71, 72, 73, 74, 75, 76, 81, 82, 84, 92, 94, 95, 101, 107]
EXPECTED_FRAMES = 25_181_516
TICKS_PER_SAMPLE = 5_292_000

RN_MATCH = "c6762838-e451-4d22-9c90-b0eaa734605a"
EQ_MATCH = "8edcd326-33de-4a4d-985b-f1bf48ed1427"
LIMITER_MATCH = "e0b23f05-f1a7-4ef7-9b50-7ec3e3002058"
EQ_DONOR_ID = "705"
LIMITER_DONOR_ID = "706"
EQ_PARAM_NAMES = [
    "Bypass", "Low Shelf Frequency", "Low Shelf Gain", "High Shelf Frequency", "High Shelf Gain",
    "EQ Band 1 Center Frequency", "EQ Band 1 Q", "EQ Band 1 Gain", "EQ Band 1 Enable",
    "EQ Band 2 Center Frequency", "EQ Band 2 Q", "EQ Band 2 Gain", "EQ Band 2 Enable",
    "EQ Band 3 Center Frequency", "EQ Band 3 Q", "EQ Band 3 Gain", "EQ Band 3 Enable",
    "EQ Band 4 Center Frequency", "EQ Band 4 Q", "EQ Band 4 Gain", "EQ Band 4 Enable",
    "EQ Band 5 Center Frequency", "EQ Band 5 Q", "EQ Band 5 Gain", "EQ Band 5 Enable",
    "Gain", "ConstantQ", "Low Shelf Filter Order", "High Shelf Filter Order", "Ultra-Quiet Mode",
    "Low Shelf Enable", "High Shelf Enable", "Original Sample Rate",
    "EQ Band 1 Bandwidth", "EQ Band 2 Bandwidth", "EQ Band 3 Bandwidth", "EQ Band 4 Bandwidth", "EQ Band 5 Bandwidth",
    "High-Pass Filter Enable", "High-Pass Cutoff Frequency", "High-Pass Slope",
    "Low-Pass Filter Enable", "Low-Pass Cutoff Frequency", "Low-Pass Slope", "Bypass", "LowGraphRange",
]
LIMITER_PARAM_NAMES = ["Bypass", "Maximum Amplitude", "Input Boost", "Look-Ahead Time", "Release Time", "Link Channels", "Decay to Ceiling", "Limit True Peak"]


class GuardError(RuntimeError):
    pass


def require(ok: bool, message: str) -> None:
    if not ok:
        raise GuardError(message)


def finite(value: object, name: str) -> float:
    require(type(value) in (int, float), f"{name}: numero exigido")
    result = float(value)
    require(math.isfinite(result), f"{name}: valor nao finito")
    return result


def freq_native(hz: float) -> float:
    require(20.0 <= hz <= 24_000.0, f"frequencia fora de 20..24000 Hz: {hz}")
    return (hz - 20.0) / 23_980.0


def gain_native(db: float) -> float:
    require(-24.0 <= db <= 24.0, f"ganho EQ fora de -24..24 dB: {db}")
    return 0.5 + db / 96.0


def read_curve(path: Path) -> dict[str, object]:
    require(path.is_file() and not path.is_symlink(), f"curva ausente/insegura: {path}")
    data = json.loads(path.read_text(encoding="utf-8"))
    require(isinstance(data, dict), "raiz da curva nao e objeto")
    require(data.get("schema") == "codex.premiere.eq-curve.v1", "schema da curva divergente")
    require(re.fullmatch(r"[A-Z0-9_]{1,48}", str(data.get("candidate_label", ""))) is not None, "candidate_label invalido")
    require(re.fullmatch(r"[A-Za-z0-9_]{1,100}", str(data.get("instance_name", ""))) is not None, "instance_name invalido")
    require(data.get("mode") == "constant_width", "somente Constant Width e permitido")
    require(finite(data.get("output_gain_db"), "output_gain_db") == 0.0, "output gain deve ser 0 dB")
    allowed = {"schema", "candidate_label", "instance_name", "mode", "output_gain_db", "high_pass", "low_shelf", "bell_bands", "high_shelf", "low_pass", "notes"}
    require(set(data) <= allowed, f"chaves desconhecidas na curva: {sorted(set(data)-allowed)}")
    hp = data.get("high_pass")
    ls = data.get("low_shelf")
    hs = data.get("high_shelf")
    lp = data.get("low_pass")
    bands = data.get("bell_bands")
    require(isinstance(hp, dict) and set(hp) == {"enabled", "frequency_hz", "slope_db_oct"}, "high_pass invalido")
    require(isinstance(ls, dict) and set(ls) == {"enabled", "frequency_hz", "gain_db", "filter_order", "design_q"}, "low_shelf invalido")
    require(isinstance(hs, dict) and set(hs) == {"enabled"}, "high_shelf invalido")
    require(isinstance(lp, dict) and set(lp) == {"enabled"}, "low_pass invalido")
    require(hp["enabled"] is True and finite(hp["slope_db_oct"], "HP slope") in {6, 12, 18, 24, 30, 36}, "HP deve estar ligado com slope valido")
    freq_native(finite(hp["frequency_hz"], "HP frequency"))
    require(ls["enabled"] is True and finite(ls["filter_order"], "low shelf order") == 1.0, "Low Shelf deve usar Filter Order=1")
    freq_native(finite(ls["frequency_hz"], "low shelf frequency")); gain_native(finite(ls["gain_db"], "low shelf gain")); finite(ls["design_q"], "low shelf design_q")
    require(hs["enabled"] is False and lp["enabled"] is False, "High Shelf e Low-pass devem estar OFF")
    require(isinstance(bands, list) and len(bands) == 5, "bell_bands deve conter exatamente 5 bandas")
    for index, band in enumerate(bands, 1):
        require(isinstance(band, dict), f"bell {index} invalida")
        if band.get("enabled") is True:
            require(set(band) == {"enabled", "frequency_hz", "gain_db", "q"}, f"bell {index}: chaves invalidas")
            hz = finite(band["frequency_hz"], f"bell {index} frequency"); q = finite(band["q"], f"bell {index} q")
            require(0.1 <= q <= 20.0, f"bell {index}: Q fora de faixa")
            require(20.0 <= hz / q <= 10_000.0, f"bell {index}: Width fora de 20..10000 Hz")
            freq_native(hz); gain_native(finite(band["gain_db"], f"bell {index} gain"))
        else:
            require(band == {"enabled": False}, f"bell {index} OFF deve conter somente enabled")
    return data


def refs(effect: ET.Element) -> list[str]:
    result = [node.get("ObjectRef") or "" for node in effect.findall("./AudioComponent/Component/Params/Param")]
    require(all(result), "ref de parametro vazia")
    return result


def top(root: ET.Element, tag: str, object_id: str) -> ET.Element:
    hits = [node for node in root if node.tag == tag and node.get("ObjectID") == object_id]
    require(len(hits) == 1, f"{tag} {object_id}: esperado 1, obtido {len(hits)}")
    return hits[0]


def decode_opaque(effect: ET.Element, count: int) -> tuple[ET.Element, str, list[str]]:
    opaque = effect.find("OpaqueData")
    require(opaque is not None and opaque.get("Encoding") == "base64" and (opaque.text or "").strip(), "OpaqueData materializado ausente")
    decoded = base64.b64decode("".join((opaque.text or "").split())).decode("utf-8")
    values = re.findall(r"<float>([^<]+)</float>", decoded)
    require(len(values) == count, f"Opaque floats={len(values)}, esperado {count}")
    return opaque, decoded, values


def eq_values(curve: dict[str, object], donor_values: list[str]) -> tuple[list[float], list[dict[str, float]]]:
    values = [0.0] + [float(value) for value in donor_values[:-1]]
    require(len(values) == 46 and math.isclose(float(donor_values[-1]), 1.0), "payload EQ donor invalido")
    hp = curve["high_pass"]; ls = curve["low_shelf"]; bands = curve["bell_bands"]
    assert isinstance(hp, dict) and isinstance(ls, dict) and isinstance(bands, list)
    values[1] = freq_native(float(ls["frequency_hz"])); values[2] = gain_native(float(ls["gain_db"])); values[30] = 1.0; values[27] = 1.0
    values[4] = 0.5; values[31] = 0.0
    rows: list[dict[str, float]] = []
    for slot, band in enumerate(bands):
        base = 5 + slot * 4
        if band.get("enabled") is True:
            hz = float(band["frequency_hz"]); q = float(band["q"]); width = hz / q
            values[base] = freq_native(hz); values[base+1] = q / 10_000.0; values[base+2] = gain_native(float(band["gain_db"])); values[base+3] = 1.0; values[33+slot] = width / 10_000.0
            rows.append({"slot": slot+1, "frequency_hz": hz, "gain_db": float(band["gain_db"]), "q": q, "width_hz": width})
        else:
            values[base+2] = 0.5; values[base+3] = 0.0
    values[25] = 0.66666698455810547; values[26] = 0.0; values[29] = 0.0
    values[38] = 1.0; values[39] = freq_native(float(hp["frequency_hz"])); values[40] = float(hp["slope_db_oct"]) / 60.0
    values[41] = 0.0; values[44] = 0.0; values[45] = 0.0
    return values, rows


def rewrite_eq(effect: ET.Element, values: list[float]) -> str:
    opaque, decoded, old = decode_opaque(effect, 46)
    new = [fmt(value) for value in values[1:]] + [fmt(float(old[-1]))]
    iterator = iter(new)
    updated, count = re.subn(r"<float>[^<]+</float>", lambda _: f"<float>{next(iterator)}</float>", decoded)
    require(count == 46, "reescrita Opaque EQ incompleta")
    opaque.text = base64.b64encode(updated.encode()).decode("ascii")
    opaque.set("BinaryHash", str(uuid.uuid5(uuid.NAMESPACE_URL, safe.sha256_bytes(updated.encode()))))
    return safe.sha256_bytes(updated.encode())


def clone_eq(donor: ET.Element, donor_params: list[ET.Element], effect_id: str, param_ids: list[str], component_id: str, instance: str, values: list[float]) -> tuple[ET.Element, list[ET.Element], str]:
    effect = copy.deepcopy(donor); effect.set("ObjectID", effect_id)
    component = effect.find("./AudioComponent/Component"); require(component is not None, "Component EQ ausente")
    component.find("ID").text = component_id; component.find("InstanceName").text = instance
    for node, value in zip(effect.findall("./AudioComponent/Component/Params/Param"), param_ids): node.set("ObjectRef", value)
    opaque_sha = rewrite_eq(effect, values)
    params: list[ET.Element] = []
    for index, (source, object_id, value) in enumerate(zip(donor_params, param_ids, values)):
        clone = copy.deepcopy(source); clone.set("ObjectID", object_id)
        if index == 0:
            for tag in ("StartKeyframe", "CurrentValue"):
                node = clone.find(tag)
                if node is not None: clone.remove(node)
        else:
            set_param_scalar(clone, value)
        params.append(clone)
    return effect, params, opaque_sha


def clone_limiter(donor: ET.Element, donor_params: list[ET.Element], effect_id: str, param_ids: list[str], component_id: str) -> tuple[ET.Element, list[ET.Element]]:
    effect = copy.deepcopy(donor); effect.set("ObjectID", effect_id)
    component = effect.find("./AudioComponent/Component"); require(component is not None, "Component limiter ausente")
    component.find("ID").text = component_id
    for node, value in zip(effect.findall("./AudioComponent/Component/Params/Param"), param_ids): node.set("ObjectRef", value)
    params: list[ET.Element] = []
    for source, object_id in zip(donor_params, param_ids):
        clone = copy.deepcopy(source); clone.set("ObjectID", object_id); params.append(clone)
    return effect, params


def collect_targets(root: ET.Element, manifest: dict[str, object], *, base_only: bool = True) -> tuple[ET.Element, list[dict[str, object]]]:
    objects = safe.object_map(root); sequence, items = safe.discover_items(root, objects)
    rows: list[dict[str, object]] = []; total = 0
    for expected, item in zip(EXPECTED_IDS, items):
        clip_id, _sub, clip, chain = safe.identify_item(item, objects); require(clip_id == expected, "ordem de IDs divergente")
        start = int(item.findtext("./ClipTrackItem/TrackItem/Start") or 0); end = int(item.findtext("./ClipTrackItem/TrackItem/End") or -1)
        frames = (end-start)//TICKS_PER_SAMPLE; require((end-start) % TICKS_PER_SAMPLE == 0 and frames == int(manifest["raws"][str(clip_id)]["frames"]), f"{clip_id}: timing divergiu")
        chain_refs = [node.get("ObjectRef") or "" for node in safe.indexed_components(chain)]
        matches = [safe.component_match(objects[value]) for value in chain_refs]
        expected_matches = [RN_MATCH] if base_only else [RN_MATCH, EQ_MATCH, LIMITER_MATCH]
        require(matches == expected_matches, f"{clip_id}: cadeia divergiu: {matches}")
        rn = objects[chain_refs[0]]; amount = [objects[x].findtext("CurrentValue") for x in refs(rn) if objects[x].findtext("Name") == "Amount"]
        require(len(amount) == 1 and math.isclose(float(amount[0]), 0.19637130200862885, abs_tol=1e-9), f"{clip_id}: RN amount divergiu")
        loud = clip.findtext("./Clip/Node/Properties/ESP.LoudnessSection.Analysis.ITU1770LoudnessValue"); gain = clip.findtext("Gain")
        require(loud is not None and gain is not None and math.isclose(float(loud)+20*math.log10(float(gain)), -23.0, abs_tol=2e-6), f"{clip_id}: Auto-Match divergiu")
        props = item.find("./ClipTrackItem/TrackItem/Node/Properties"); require(props is not None, f"{clip_id}: Properties ausente")
        require(props.find("ESP.EnhanceSpeechSection.1") is None and props.find("ESP.RestorationSection.5") is None, f"{clip_id}: Enhance/DeEss presente")
        total += frames; rows.append({"id": clip_id, "item": item, "clip": clip, "chain": chain, "rn": rn, "start": start, "end": end, "frames": frames, "chain_id": chain.get("ObjectID"), "rn_id": rn.get("ObjectID")})
    require(total == EXPECTED_FRAMES, "total de frames divergiu")
    return sequence, rows


def append_nodes(xml: str, nodes: list[ET.Element]) -> str:
    match = re.search(r"(?m)^</PremiereData>\s*$", xml); require(match is not None, "fecho PremiereData ausente")
    require(nodes, "nenhum objeto novo para anexar")
    # O primeiro bloco comeca sem TAB para preservar exatamente o tail do
    # ultimo objeto existente; os demais mantem a indentacao nativa.
    payload = safe.serialize_top(nodes[0]).lstrip("\t") + "".join(safe.serialize_top(node) for node in nodes[1:])
    return xml[:match.start()] + payload + "</PremiereData>\n"


def main() -> int:
    parser = argparse.ArgumentParser(); parser.add_argument("--curve", type=Path, required=True); parser.add_argument("--output", type=Path, required=True); parser.add_argument("--manifest", type=Path, required=True); args = parser.parse_args()
    output = args.output.resolve(); report_path = args.manifest.resolve()
    require(output.parent == PROJECTS and output != INPUT and not output.exists(), "output invalido/ocupado")
    require(report_path.parent == (TRIAL / "analises").resolve() and not report_path.exists(), "manifesto invalido/ocupado")
    curve = read_curve(args.curve.resolve())
    require(safe.sha256_file(INPUT) == EXPECTED_INPUT_SHA, "SHA da entrada divergiu"); require(safe.sha256_file(DONOR) == EXPECTED_DONOR_SHA, "SHA do donor divergiu"); require(safe.sha256_file(PREFLIGHT) == EXPECTED_PREFLIGHT_SHA, "SHA preflight divergiu")
    manifest = json.loads(PREFLIGHT.read_text(encoding="utf-8")); require([int(x) for x in manifest["fixed_order"]] == EXPECTED_IDS, "preflight IDs divergiu")
    input_bytes = INPUT.read_bytes(); xml_before = gzip.decompress(input_bytes).decode(); root_before = ET.fromstring(xml_before); require(root_before.tag == "PremiereData", "XML entrada invalido")
    _sequence, rows = collect_targets(root_before, manifest); top_before = safe.top_hashes(root_before); graph_before = safe.numeric_graph(root_before)
    donor_root = ET.fromstring(gzip.decompress(DONOR.read_bytes())); donor_objects = safe.object_map(donor_root)
    donor_eq = top(donor_root, "AudioFilterComponent", EQ_DONOR_ID); donor_lim = top(donor_root, "AudioFilterComponent", LIMITER_DONOR_ID)
    require(safe.component_match(donor_eq) == EQ_MATCH and safe.component_match(donor_lim) == LIMITER_MATCH, "donor de efeitos divergiu")
    eq_param_ids = refs(donor_eq); lim_param_ids = refs(donor_lim); eq_params = [donor_objects[x] for x in eq_param_ids]; lim_params = [donor_objects[x] for x in lim_param_ids]
    require([x.findtext("Name") for x in eq_params] == EQ_PARAM_NAMES and [x.findtext("Name") for x in lim_params] == LIMITER_PARAM_NAMES, "params donor divergiram")
    _, _, old_eq_values = decode_opaque(donor_eq, 46); _, limiter_decoded, limiter_values = decode_opaque(donor_lim, 8)
    require(limiter_values == ["0.99699998", "0.72066665", "0", "1", "1", "1", "0", "2"], "limiter v1 donor divergiu")
    values, bell_rows = eq_values(curve, old_eq_values)
    existing = {int(node.get("ObjectID")) for node in root_before if node.get("ObjectID") and node.get("ObjectID").isdigit()}; next_id = max(existing)+1
    root_work = copy.deepcopy(root_before); _seq_work, work_rows = collect_targets(root_work, manifest); added: list[ET.Element] = []; per_clip: list[dict[str, object]] = []; opaque_shas: set[str] = set()
    for row in work_rows:
        chain = row["chain"]; rn = row["rn"]; rn_internal = int(rn.findtext("./AudioComponent/Component/ID") or 0)
        eq_id = str(next_id); eq_pids = [str(x) for x in range(next_id+1, next_id+47)]; lim_id = str(next_id+47); lim_pids = [str(x) for x in range(next_id+48, next_id+56)]; next_id += 56
        eq, new_eq_params, opaque_sha = clone_eq(donor_eq, eq_params, eq_id, eq_pids, str(rn_internal+1), str(curve["instance_name"]), values); lim, new_lim_params = clone_limiter(donor_lim, lim_params, lim_id, lim_pids, str(rn_internal+2)); opaque_shas.add(opaque_sha)
        container = chain.find("./ComponentChain/Components"); require(container is not None and len(container) == 1, "chain container divergente")
        container.append(ET.Element("Component", {"Index":"1", "ObjectRef":eq_id})); container.append(ET.Element("Component", {"Index":"2", "ObjectRef":lim_id}))
        added.extend([eq, *new_eq_params, lim, *new_lim_params]); per_clip.append({"id":row["id"], "chain_id":row["chain_id"], "rn_id":row["rn_id"], "eq_id":eq_id, "limiter_id":lim_id})
    require(len(added) == 1120 and len(opaque_shas) == 1, "contagem/payload EQ divergente")
    changed_chains = [row["chain"] for row in work_rows]; xml_after, block_digests = safe.surgical_replace(xml_before, changed_chains); xml_after = append_nodes(xml_after, added); xml_after_bytes = xml_after.encode(); root_after = ET.fromstring(xml_after_bytes); objects_after = safe.object_map(root_after)
    _seq_after, final_rows = collect_targets(root_after, manifest, base_only=False)
    for row in final_rows:
        chain_refs = [x.get("ObjectRef") or "" for x in safe.indexed_components(row["chain"])]; require([safe.component_match(objects_after[x]) for x in chain_refs] == [RN_MATCH, EQ_MATCH, LIMITER_MATCH], f"{row['id']}: ordem final invalida")
    top_after = safe.top_hashes(root_after); changed = sorted(k for k in top_before if top_before[k] != top_after[k]); expected_changed = sorted(safe.label(x) for x in changed_chains); require(changed == expected_changed, f"whitelist changed divergiu: {changed}")
    added_keys = sorted(set(top_after)-set(top_before)); require(len(added_keys) == 1120 and not (set(top_before)-set(top_after)), "whitelist added/removed divergiu")
    graph_after = safe.numeric_graph(root_after); require(graph_after["object_id_count"] == graph_before["object_id_count"]+1120 and graph_after["object_ref_count"] == graph_before["object_ref_count"]+1120, "contagem do grafo divergiu"); require(graph_after["duplicate_object_ids"] == graph_before["duplicate_object_ids"] and graph_after["unresolved_object_refs"] == graph_before["unresolved_object_refs"], "integridade do grafo divergiu")
    compressed = gzip.compress(xml_after_bytes, compresslevel=9, mtime=0); output_sha = safe.sha256_bytes(compressed)
    report = {"schema":"codex.premiere.safe-prproj-mutation.rn20-eq-limiter20.v1", "created_utc":datetime.now(timezone.utc).isoformat(), "status":"C1_OFFLINE_PASS_REQUIRES_PREMIERE_ROUNDTRIP_AND_REAL_EXPORT", "input":{"path":str(INPUT),"sha256":EXPECTED_INPUT_SHA,"modified":False}, "donor":{"path":str(DONOR),"sha256":EXPECTED_DONOR_SHA,"eq_object_id":EQ_DONOR_ID,"limiter_object_id":LIMITER_DONOR_ID}, "curve":curve, "derived_bells":bell_rows, "eq_native_values":values, "eq_opaque_decoded_sha256":next(iter(opaque_shas)), "limiter_opaque_decoded_sha256":safe.sha256_bytes(limiter_decoded.encode()), "output":{"path":str(output),"sha256":output_sha,"bytes":len(compressed)}, "timeline":{"ids":EXPECTED_IDS,"total_frames":EXPECTED_FRAMES,"unchanged":True,"audio_files_opened":False}, "chain":{"before":["Essential Reduce Noise RN20"],"after":["Essential Reduce Noise RN20",str(curve["instance_name"]),"DJI_FEMININA_CAPCUT_BASE__LIMITER_FINAL_v1"],"per_clip":per_clip}, "isolation":{"changed_top_level_objects":changed,"added_top_level_object_count":len(added_keys),"removed_top_level_objects":[],"all_other_objects_unchanged":True,"block_digests":block_digests,"graph_before":graph_before,"graph_after":graph_after}, "next_step":"Open/resave no Premiere, auditar round-trip e exportar WAV real antes de qualquer aprovacao."}
    safe.atomic_create(output, compressed); require(safe.sha256_file(output) == output_sha, "readback output divergiu"); require(safe.sha256_file(INPUT) == EXPECTED_INPUT_SHA and safe.sha256_file(DONOR) == EXPECTED_DONOR_SHA, "entrada/donor mudou")
    safe.atomic_create(report_path, (json.dumps(report, ensure_ascii=False, indent=2)+"\n").encode()); print(json.dumps({"status":report["status"],"output":report["output"],"changed_chains":len(changed),"added_objects":len(added_keys),"audio_files_opened":False}, ensure_ascii=False, indent=2)); return 0


if __name__ == "__main__":
    try: raise SystemExit(main())
    except (GuardError, safe.GuardError, OSError, ValueError, KeyError, json.JSONDecodeError, ET.ParseError) as exc:
        print(f"ERRO DE TRAVA: {exc}", file=sys.stderr); raise SystemExit(2)

#!/usr/bin/env python3
"""Audite, sem mutacao, a calibracao Essential Sound SEM Enhance em 20 clipes.

O contrato e deliberadamente fail-closed. O auditor aceita somente a sequencia
congelada ``FLUXO_APROVADO_20``, as 20 fontes/hash/timings do preflight e, na
cadeia de cada clipe, Reduce Noise mais um DeEss Essential opcional. Efeitos de
faixa/master, Enhance, Rumble, DeHum, Reverb, Clarity/Dynamics, EQ e limiter
fazem a auditoria falhar.

O arquivo .prproj e todas as midias sao apenas lidos. Nenhum relatorio e salvo:
a saida vai para stdout e o codigo de retorno e 0 em PASS ou 2 em FAIL.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import math
import re
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence


WORKSPACE = Path(__file__).resolve().parents[2]
NATIVE = WORKSPACE / "premiere_audition_native_preset"
PROJECT_ROOT = (NATIVE / "projetos_descartaveis").resolve()
SUITE = (NATIVE / "comparacao_ab" / "FLUXO_APROVADO_20_20260814").resolve()
RAW_ROOT = (SUITE / "raws_ordered").resolve()
MANIFEST = (SUITE / "manifest_preflight.json").resolve()
MANIFEST_SHA256 = "9989b1fb65798053621c1e342a95dc6f75a7dcdf134f16ed162745b237316ccd"

SEQUENCE_NAME = "FLUXO_APROVADO_20"
EXPECTED_IDS = (64, 65, 66, 67, 68, 69, 71, 72, 73, 74, 75, 76, 81, 82, 84, 92, 94, 95, 101, 107)
EXCLUDED_IDS = frozenset({21, 38, 39, 51, 70, 77, 83})
EXPECTED_TOTAL_FRAMES = 25_181_516

PREMIERE_TICKS_PER_SECOND = 254_016_000_000
SAMPLE_RATE = 48_000
TICKS_PER_SAMPLE = PREMIERE_TICKS_PER_SECOND // SAMPLE_RATE
STEREO_LAYOUT = [{"channellabel": 100}, {"channellabel": 101}]
NEUTRAL_INTERNAL_LEVEL = 0.17782793939113617

ENHANCE_MATCH = "7f446972-835b-422e-a8a3-d64028839ba2"
DENOISE_MATCH = "c6762838-e451-4d22-9c90-b0eaa734605a"
DEESS_MATCH = "ffbe710f-cd69-4139-ad26-65603616d9d4"
PARAMETRIC_EQ_MATCH = "8edcd326-33de-4a4d-985b-f1bf48ed1427"
HARD_LIMITER_MATCH = "e0b23f05-f1a7-4ef7-9b50-7ec3e3002058"
DYNAMICS_MATCH = "f76dad9c-77da-477a-9939-eefefc5c23bd"
INTERNAL_VOLUME = "Internal Volume Stereo"
INTERNAL_CHANNEL_VOLUME = "Internal Channel Volume Stereo"

DENOISE_EFFECT_ORDER = 20
DEFAULT_DENOISE_AMOUNT = 0.19637130200862885
DEFAULT_AMOUNT_TOLERANCE = 1.0e-7
PARAM_ZERO_TOLERANCE = 1.0e-9
AUTOMATCH_GAIN_ABS_TOLERANCE = 5.0e-8
AUTOMATCH_GAIN_REL_TOLERANCE = 1.0e-7
AUTOMATCH_LUFS_TOLERANCE = 5.0e-6

LOUDNESS_PROPERTIES = {
    "ESP.LoudnessSection.Analysis.TimeOut",
    "ESP.LoudnessSection.Analysis.TimeIn",
    "ESP.LoudnessSection.Analysis.ContentState",
    "ESP.LoudnessSection.Analysis.ITU1770LoudnessValue",
}

ID_TOKEN = re.compile(r"(?<!\d)(\d{2,3})(?!\d)")

# Os sete primeiros valores do vetor Adobe ReduceSibilanceMin/MaxSetting sao
# os parametros fisicos Threshold, Attack, Release, Center, Bandwidth, Output e
# Mode. Eles permitem validar um valor de UI sem depender da legenda mostrada.
DEESS_MIN = (0.875, 0.1998399794101715, 0.18367347121238708, 0.19130434095859528, 0.2567567527294159, 0.0, 1.0)
DEESS_MAX = (0.5, 0.1998399794101715, 0.18367347121238708, 0.260869562625885, 0.5270270109176636, 0.0, 1.0)
DEESS_NUMERIC_NAMES = (
    "Threshold",
    "Attack",
    "Release",
    "Center Frequency",
    "Bandwidth",
    "Output Sibilance Only",
)

FILTER_LABELS = {
    ENHANCE_MATCH: "Enhance Speech",
    DENOISE_MATCH: "Reduce Noise/DeNoise",
    DEESS_MATCH: "DeEss",
    PARAMETRIC_EQ_MATCH: "Parametric EQ",
    HARD_LIMITER_MATCH: "Hard Limiter",
    DYNAMICS_MATCH: "Dynamics",
    INTERNAL_VOLUME: "Internal Volume",
    INTERNAL_CHANNEL_VOLUME: "Internal Channel Volume",
}


class GuardError(RuntimeError):
    """Uma pre-condicao estrutural ou de integridade falhou."""


def require(condition: bool, message: str) -> None:
    if not condition:
        raise GuardError(message)


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(4 * 1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def is_within(path: Path, parent: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(parent.resolve(strict=False))
        return True
    except ValueError:
        return False


@dataclass(frozen=True)
class Snapshot:
    size: int
    mtime_ns: int
    sha256: str


def snapshot(path: Path) -> Snapshot:
    stat = path.stat()
    return Snapshot(stat.st_size, stat.st_mtime_ns, sha256_file(path))


def object_map(root: ET.Element) -> dict[str, ET.Element]:
    result: dict[str, ET.Element] = {}
    for node in root:
        for attribute in ("ObjectID", "ObjectUID"):
            value = node.get(attribute)
            if value is None:
                continue
            require(value not in result, f"identidade top-level duplicada: {value}")
            result[value] = node
    return result


def resolve(objects: Mapping[str, ET.Element], value: str | None, context: str) -> ET.Element:
    require(value is not None, f"{context}: referencia ausente")
    require(value in objects, f"{context}: referencia orfa {value}")
    return objects[value]


def indexed_refs(nodes: Iterable[ET.Element], attribute: str) -> list[str]:
    pairs: list[tuple[int, str]] = []
    for position, node in enumerate(nodes):
        value = node.get(attribute)
        require(value is not None, f"referencia {attribute} ausente")
        pairs.append((int(node.get("Index", position)), value))
    pairs.sort()
    require([index for index, _ in pairs] == list(range(len(pairs))), f"indices nao sequenciais: {[index for index, _ in pairs]}")
    return [value for _, value in pairs]


def properties(node: ET.Element | None, context: str) -> dict[str, str]:
    require(node is not None, f"{context}: Properties ausente")
    result: dict[str, str] = {}
    for child in list(node):
        require(child.tag not in result, f"{context}: propriedade duplicada {child.tag}")
        result[child.tag] = child.text or ""
    return result


def closure(start: ET.Element, objects: Mapping[str, ET.Element]) -> set[str]:
    roots = [value for attribute in ("ObjectID", "ObjectUID") if (value := start.get(attribute))]
    require(roots, f"{start.tag}: objeto inicial sem identidade")
    seen: set[str] = set()
    pending = roots[:]
    while pending:
        current = pending.pop()
        if current in seen:
            continue
        seen.add(current)
        for child in objects[current].iter():
            for attribute in ("ObjectRef", "ObjectURef"):
                value = child.get(attribute)
                if value in objects and value not in seen:
                    pending.append(value)
    return seen


def finite_float(value: str | None, context: str) -> float:
    require(value is not None and value != "", f"{context}: valor ausente")
    try:
        number = float(value)
    except ValueError as error:
        raise GuardError(f"{context}: valor nao numerico {value!r}") from error
    require(math.isfinite(number), f"{context}: valor nao finito {number}")
    return number


def property_is_enabled(props: Mapping[str, str], key: str) -> bool:
    value = props.get(key)
    if value is None or value == "":
        return False
    lowered = value.strip().lower()
    if lowered in {"false", "off", "no"}:
        return False
    if lowered in {"true", "on", "yes"}:
        return True
    try:
        return not math.isclose(float(value), 0.0, abs_tol=PARAM_ZERO_TOLERANCE)
    except ValueError:
        return True


def parameter_value(
    parameter: ET.Element,
    context: str,
    *,
    required: bool,
    allow_missing_static_marker: bool = False,
) -> float | None:
    varying = parameter.findtext("IsTimeVarying")
    expected_markers = {"false", None} if allow_missing_static_marker else {"false"}
    require(varying in expected_markers, f"{context}: parametro possui automacao ou marcador invalido ({varying!r})")
    require(parameter.find("Keyframes") is None, f"{context}: parametro possui keyframes")
    current_text = parameter.findtext("CurrentValue")
    start_text = parameter.findtext("StartKeyframe")
    current = finite_float(current_text, f"{context} CurrentValue") if current_text is not None else None
    start: float | None = None
    if start_text:
        fields = start_text.split(",")
        require(len(fields) >= 2, f"{context}: StartKeyframe invalido")
        start = finite_float(fields[1], f"{context} StartKeyframe")
    if required:
        require(current is not None, f"{context}: CurrentValue ausente")
        require(start is not None, f"{context}: StartKeyframe ausente")
    if current is not None and start is not None:
        require(math.isclose(current, start, rel_tol=0.0, abs_tol=5.0e-10), f"{context}: CurrentValue {current} != StartKeyframe {start}")
    return current if current is not None else start


def component_parameters(
    component: ET.Element,
    objects: Mapping[str, ET.Element],
    expected_names: set[str],
    context: str,
) -> dict[str, ET.Element]:
    refs = indexed_refs(component.findall("./AudioComponent/Component/Params/Param"), "ObjectRef")
    result: dict[str, ET.Element] = {}
    for value in refs:
        parameter = resolve(objects, value, f"{context} parametro")
        require(parameter.tag == "AudioComponentParam", f"{context}: parametro aponta para {parameter.tag}")
        name = parameter.findtext("Name") or ""
        require(name and name not in result, f"{context}: parametro ausente/duplicado {name!r}")
        result[name] = parameter
    require(set(result) == expected_names, f"{context}: parametros {sorted(result)}; esperado {sorted(expected_names)}")
    return result


def audit_stereo_component(component: ET.Element, context: str) -> None:
    layout_text = component.findtext("./AudioComponent/AudioChannelLayout")
    require(layout_text is not None, f"{context}: AudioChannelLayout ausente")
    try:
        layout = json.loads(layout_text)
    except json.JSONDecodeError as error:
        raise GuardError(f"{context}: AudioChannelLayout invalido") from error
    require(layout == STEREO_LAYOUT, f"{context}: layout nao stereo {layout}")
    require(component.findtext("./AudioComponent/ChannelType") == "1", f"{context}: ChannelType nao stereo")
    require(component.findtext("./AudioComponent/FrameRate") == str(TICKS_PER_SAMPLE), f"{context}: FrameRate nao e 48 kHz")
    config_text = component.findtext("ChannelConfigData")
    if config_text:
        try:
            config = json.loads(config_text)
        except json.JSONDecodeError as error:
            raise GuardError(f"{context}: ChannelConfigData invalido") from error
        layouts = [entry.get("layout") for side in ("in", "out") for entry in config.get(side, [])]
        require(layouts and all(value == [100, 101] for value in layouts), f"{context}: ChannelConfigData nao stereo")


def component_match(component: ET.Element) -> str:
    return component.findtext("FilterMatchName") or ""


def component_inner_id(component: ET.Element, context: str) -> str:
    value = component.findtext("./AudioComponent/Component/ID")
    require(value is not None and value != "", f"{context}: Component/ID ausente")
    return value


def component_effect_order(component: ET.Element, context: str) -> int:
    value = component.findtext("./AudioComponent/Component/Node/Properties/EssentialSound.EffectOrder")
    require(value is not None, f"{context}: marcador EssentialSound.EffectOrder ausente")
    try:
        return int(value)
    except ValueError as error:
        raise GuardError(f"{context}: EffectOrder invalido {value!r}") from error


def audit_intrinsic(component: ET.Element, objects: Mapping[str, ET.Element], match: str, context: str) -> None:
    require(component.tag == "AudioFilterComponent", f"{context}: intrinseco nao e AudioFilterComponent")
    require(component.findtext("./AudioComponent/Component/Intrinsic") == "true", f"{context}: intrinseco nao marcado")
    expected = {"Bypass", "Level", "Mute"} if match == INTERNAL_VOLUME else {"Bypass", "Left", "Right", "Mute"}
    params = component_parameters(component, objects, expected, context)
    names = ("Level",) if match == INTERNAL_VOLUME else ("Left", "Right")
    for name in names:
        value = parameter_value(params[name], f"{context} {name}", required=True)
        require(value is not None and math.isclose(value, NEUTRAL_INTERNAL_LEVEL, abs_tol=1.0e-7), f"{context}: {name} nao neutro ({value})")
    for name in ("Bypass", "Mute"):
        value = parameter_value(params[name], f"{context} {name}", required=False)
        require(value is None or math.isclose(value, 0.0, abs_tol=PARAM_ZERO_TOLERANCE), f"{context}: {name} ativo ({value})")


def audit_denoise(
    component: ET.Element,
    objects: Mapping[str, ET.Element],
    expected_amount: float,
    tolerance: float,
    context: str,
) -> dict[str, float]:
    require(component.tag == "AudioFilterComponent", f"{context}: DeNoise nao e AudioFilterComponent")
    require(component_match(component) == DENOISE_MATCH, f"{context}: GUID DeNoise divergente")
    audit_stereo_component(component, context)
    require(component_effect_order(component, context) == DENOISE_EFFECT_ORDER, f"{context}: EffectOrder nao e {DENOISE_EFFECT_ORDER}")
    require(component.findtext("FilterPreset") == "0", f"{context}: FilterPreset nao e 0")
    require(component.findtext("FilterIndex") == "-1", f"{context}: FilterIndex nao e -1")

    expected_names = {"Bypass", "Amount", "Make-Up Gain", "Auto Gain", "Shelve Type", "Output Difference Signal"}
    params = component_parameters(component, objects, expected_names, context)
    bypass = parameter_value(params["Bypass"], f"{context} Bypass", required=False)
    require(bypass is None or math.isclose(bypass, 0.0, abs_tol=PARAM_ZERO_TOLERANCE), f"{context}: Bypass ativo ({bypass})")
    values: dict[str, float] = {}
    for name in expected_names - {"Bypass"}:
        value = parameter_value(params[name], f"{context} {name}", required=True)
        assert value is not None
        values[name] = value
    require(math.isclose(values["Amount"], expected_amount, rel_tol=0.0, abs_tol=tolerance), f"{context}: Amount={values['Amount']:.12g}; esperado {expected_amount:.12g} +/- {tolerance:g}")
    for name in ("Make-Up Gain", "Auto Gain", "Shelve Type", "Output Difference Signal"):
        require(math.isclose(values[name], 0.0, rel_tol=0.0, abs_tol=PARAM_ZERO_TOLERANCE), f"{context}: {name}={values[name]}; esperado 0")
    return values


def expected_deess_values(ui_amount: float) -> dict[str, float]:
    fraction = ui_amount / 10.0
    vector = [minimum + fraction * (maximum - minimum) for minimum, maximum in zip(DEESS_MIN, DEESS_MAX)]
    return dict(zip((*DEESS_NUMERIC_NAMES, "Mode"), vector))


def audit_deess(
    component: ET.Element,
    objects: Mapping[str, ET.Element],
    expected_ui: float | None,
    expected_effect_order: int | None,
    context: str,
) -> tuple[dict[str, float | None], int]:
    require(component.tag == "AudioFilterComponent", f"{context}: DeEss nao e AudioFilterComponent")
    require(component_match(component) == DEESS_MATCH, f"{context}: GUID DeEss divergente")
    audit_stereo_component(component, context)
    order = component_effect_order(component, context)
    require(DENOISE_EFFECT_ORDER < order < 100, f"{context}: EffectOrder={order}; esperado entre DeNoise e Volume")
    if expected_effect_order is not None:
        require(order == expected_effect_order, f"{context}: EffectOrder={order}; esperado {expected_effect_order}")

    names = {"Bypass", "Threshold", "Attack", "Release", "Center Frequency", "Bandwidth", "Output Sibilance Only", "Mode"}
    params = component_parameters(component, objects, names, context)
    # O DeEsser nativo omite IsTimeVarying nos parametros estaticos; a
    # ausencia de Keyframes continua sendo exigida pelo leitor fail-closed.
    bypass = parameter_value(
        params["Bypass"],
        f"{context} Bypass",
        required=False,
        allow_missing_static_marker=True,
    )
    require(bypass is None or math.isclose(bypass, 0.0, abs_tol=PARAM_ZERO_TOLERANCE), f"{context}: Bypass ativo ({bypass})")
    values: dict[str, float | None] = {}
    for name in DEESS_NUMERIC_NAMES:
        values[name] = parameter_value(
            params[name],
            f"{context} {name}",
            required=True,
            allow_missing_static_marker=True,
        )
    values["Mode"] = parameter_value(
        params["Mode"],
        f"{context} Mode",
        required=False,
        allow_missing_static_marker=True,
    )
    require(values["Output Sibilance Only"] is not None and math.isclose(float(values["Output Sibilance Only"]), 0.0, abs_tol=PARAM_ZERO_TOLERANCE), f"{context}: Output Sibilance Only deve ser 0")
    require(values["Mode"] is None or math.isclose(float(values["Mode"]), 1.0, abs_tol=PARAM_ZERO_TOLERANCE), f"{context}: Mode deve ser Multiband/1 ou serializacao neutra")

    if expected_ui is not None:
        expected = expected_deess_values(expected_ui)
        for name in DEESS_NUMERIC_NAMES:
            actual = values[name]
            require(actual is not None and math.isclose(float(actual), expected[name], rel_tol=0.0, abs_tol=5.0e-7), f"{context}: {name}={actual}; esperado {expected[name]:.12g} para UI {expected_ui}")
    return values, order


@dataclass
class Target:
    clip_id: int
    item: ET.Element
    subclip: ET.Element
    clip: ET.Element
    chain: ET.Element
    start: int
    end: int
    frames: int
    media_path: Path
    row: dict[str, Any]


def load_manifest() -> dict[str, Any]:
    require(MANIFEST.is_file() and not MANIFEST.is_symlink(), f"manifesto ausente/inseguro: {MANIFEST}")
    actual_sha = sha256_file(MANIFEST)
    require(actual_sha == MANIFEST_SHA256, f"SHA do manifesto divergiu: {actual_sha}")
    try:
        payload = json.loads(MANIFEST.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise GuardError(f"manifesto invalido: {error}") from error
    require(payload.get("schema_version") == 1, "schema_version do manifesto nao e 1")
    require(payload.get("suite") == "fluxo_aprovado_20_20260814", "suite do manifesto divergente")
    require(payload.get("fixed_order") == [str(value) for value in EXPECTED_IDS], "ordem fixa do manifesto divergente")
    require(payload.get("counts") == {"pairs": 20, "raws": 20, "targets": 20}, "contagens do manifesto divergentes")
    require(payload.get("preflight_status") == "PASS_20_OF_20", "preflight do manifesto nao esta PASS_20_OF_20")
    require(int(payload.get("raws_total_frames", -1)) == EXPECTED_TOTAL_FRAMES, "total de frames do manifesto divergente")
    scope = payload.get("scope")
    require(isinstance(scope, dict), "scope ausente no manifesto")
    require(scope.get("authorized_ids_only") == [str(value) for value in EXPECTED_IDS], "IDs autorizados do manifesto divergentes")
    require({int(value) for value in scope.get("explicitly_excluded_ids", [])} == EXCLUDED_IDS, "IDs excluidos do manifesto divergentes")
    raws = payload.get("raws")
    require(isinstance(raws, dict) and set(raws) == {str(value) for value in EXPECTED_IDS}, "mapa raws do manifesto divergente")
    return payload


def load_project(path: Path) -> tuple[ET.Element, dict[str, ET.Element], Snapshot, Path]:
    require(not path.is_symlink(), f"projeto nao pode ser link simbolico: {path}")
    resolved = path.resolve()
    require(is_within(resolved, PROJECT_ROOT), f"projeto fora de {PROJECT_ROOT}: {resolved}")
    require(resolved.is_file(), f"projeto inexistente: {resolved}")
    before = snapshot(resolved)
    try:
        raw = resolved.read_bytes()
        require(raw.startswith(b"\x1f\x8b"), "projeto nao possui assinatura gzip")
        xml = gzip.decompress(raw)
        root = ET.fromstring(xml)
    except (OSError, EOFError, ET.ParseError) as error:
        raise GuardError(f".prproj gzip/XML invalido: {error}") from error
    require(root.tag == "PremiereData", f"raiz XML inesperada: {root.tag}")
    return root, object_map(root), before, resolved


def unique_sequence(root: ET.Element, name: str) -> ET.Element:
    found = [node for node in root.findall("Sequence") if node.findtext("Name") == name]
    require(len(found) == 1, f"sequencia {name!r}: esperado 1, encontrado {len(found)}")
    return found[0]


def identify_target(item: ET.Element, objects: Mapping[str, ET.Element]) -> tuple[int, ET.Element, ET.Element, ET.Element]:
    sub_ref = item.find("./ClipTrackItem/SubClip")
    subclip = resolve(objects, sub_ref.get("ObjectRef") if sub_ref is not None else None, "SubClip")
    require(subclip.tag == "SubClip", f"SubClip aponta para {subclip.tag}")
    name = subclip.findtext("Name") or ""
    tokens = {int(value) for value in ID_TOKEN.findall(name)}
    authorized = tokens & set(EXPECTED_IDS)
    require(len(authorized) == 1, f"ID ambiguo no SubClip {name!r}: {sorted(authorized)}")
    require(not (tokens & EXCLUDED_IDS), f"ID excluido no SubClip {name!r}")
    clip_id = next(iter(authorized))
    clip_ref = subclip.find("Clip")
    clip = resolve(objects, clip_ref.get("ObjectRef") if clip_ref is not None else None, f"AudioClip {clip_id}")
    require(clip.tag == "AudioClip", f"{clip_id}: Clip aponta para {clip.tag}")
    chain_ref = item.find("./ClipTrackItem/ComponentOwner/Components")
    chain = resolve(objects, chain_ref.get("ObjectRef") if chain_ref is not None else None, f"chain {clip_id}")
    require(chain.tag == "AudioComponentChain", f"{clip_id}: chain aponta para {chain.tag}")
    return clip_id, subclip, clip, chain


def validate_sources_and_timeline(
    root: ET.Element,
    objects: Mapping[str, ET.Element],
    manifest: Mapping[str, Any],
    sequence_name: str,
) -> tuple[ET.Element, list[Target]]:
    sequence = unique_sequence(root, sequence_name)
    reachable = closure(sequence, objects)
    items = [objects[value] for value in reachable if objects[value].tag == "AudioClipTrackItem"]
    require(len(items) == 20, f"sequencia alvo alcanca {len(items)} AudioClipTrackItems; esperado 20")
    items.sort(key=lambda node: int(node.findtext("./ClipTrackItem/TrackItem/Start") or "0"))
    # Premiere omite <Start> quando o valor e zero no primeiro TrackItem.
    require(int(items[0].findtext("./ClipTrackItem/TrackItem/Start") or "0") == 0, "timeline nao comeca em zero")

    targets: list[Target] = []
    seen_clips: set[str] = set()
    seen_items: set[str] = set()
    seen_chains: set[str] = set()
    total_frames = 0
    for position, (expected_id, item) in enumerate(zip(EXPECTED_IDS, items), 1):
        clip_id, subclip, clip, chain = identify_target(item, objects)
        require(clip_id == expected_id, f"ordem {position}: ID {clip_id}; esperado {expected_id}")
        item_id = item.get("ObjectID") or ""
        clip_object_id = clip.get("ObjectID") or ""
        chain_object_id = chain.get("ObjectID") or ""
        require(item_id and item_id not in seen_items, f"{clip_id}: TrackItem ausente/repetido")
        require(clip_object_id and clip_object_id not in seen_clips, f"{clip_id}: AudioClip ausente/repetido")
        require(chain_object_id and chain_object_id not in seen_chains, f"{clip_id}: AudioComponentChain ausente/repetida")
        seen_items.add(item_id)
        seen_clips.add(clip_object_id)
        seen_chains.add(chain_object_id)

        start = int(item.findtext("./ClipTrackItem/TrackItem/Start") or "0")
        end = int(item.findtext("./ClipTrackItem/TrackItem/End") or "-1")
        require(end > start and (end - start) % TICKS_PER_SAMPLE == 0, f"{clip_id}: intervalo invalido {start}-{end}")
        if targets:
            require(start == targets[-1].end, f"gap/overlap {targets[-1].clip_id}->{clip_id}: {targets[-1].end}->{start}")
        require(item.find("./ClipTrackItem/HeadTransition") is None and item.find("./ClipTrackItem/TailTransition") is None, f"{clip_id}: Head/TailTransition presente")
        frames = (end - start) // TICKS_PER_SAMPLE
        raw = manifest["raws"][str(clip_id)]
        require(int(raw.get("order", -1)) == position, f"{clip_id}: ordem no manifesto divergente")
        require(frames == int(raw["frames"]), f"{clip_id}: {frames} frames; esperado {raw['frames']}")
        require(raw.get("sample_rate") == SAMPLE_RATE and raw.get("channels") == 2, f"{clip_id}: formato do manifesto nao e stereo 48 kHz")

        clip_in = int(clip.findtext("./Clip/InPoint") or "0")
        clip_out = int(clip.findtext("./Clip/OutPoint") or "-1")
        require(clip_in == 0 and clip_out == end - start, f"{clip_id}: fonte usa {clip_in}-{clip_out}; item dura {end-start}")
        source_ref = clip.find("./Clip/Source")
        source = resolve(objects, source_ref.get("ObjectRef") if source_ref is not None else None, f"fonte {clip_id}")
        require(source.tag == "AudioMediaSource", f"{clip_id}: fonte e {source.tag}, nao AudioMediaSource")
        media_ref = source.find("./MediaSource/Media")
        media = resolve(objects, media_ref.get("ObjectURef") if media_ref is not None else None, f"media {clip_id}")
        path_text = media.findtext("ActualMediaFilePath") or media.findtext("FilePath") or ""
        require(path_text.startswith("/"), f"{clip_id}: caminho de media nao absoluto")
        media_path = Path(path_text).resolve()
        expected_path = (SUITE / str(raw["path"])).resolve()
        require(is_within(expected_path, RAW_ROOT), f"{clip_id}: manifesto aponta fora de raws_ordered")
        require(media_path == expected_path, f"{clip_id}: media {media_path}; esperado {expected_path}")
        require(media_path.is_file() and not media_path.is_symlink(), f"{clip_id}: media ausente/insegura {media_path}")
        require(media_path.stat().st_size == int(raw["size_bytes"]), f"{clip_id}: tamanho da media divergiu")
        actual_media_sha = sha256_file(media_path)
        require(actual_media_sha == raw["file_sha256"], f"{clip_id}: SHA da media divergiu: {actual_media_sha}")

        total_frames += frames
        row = {
            "order": position,
            "id": clip_id,
            "track_item_object_id": item_id,
            "audio_clip_object_id": clip_object_id,
            "chain_object_id": chain_object_id,
            "start_ticks": start,
            "end_ticks": end,
            "frames": frames,
            "media_path": str(media_path),
            "media_sha256": actual_media_sha,
        }
        targets.append(Target(clip_id, item, subclip, clip, chain, start, end, frames, media_path, row))
    require(total_frames == EXPECTED_TOTAL_FRAMES, f"total={total_frames} frames; esperado {EXPECTED_TOTAL_FRAMES}")
    return sequence, targets


def audit_fader(component: ET.Element, objects: Mapping[str, ET.Element], context: str) -> None:
    refs = indexed_refs(component.findall("./AudioComponent/Component/Params/Param"), "ObjectRef")
    for value in refs:
        parameter = resolve(objects, value, f"{context} parametro")
        name = parameter.findtext("Name") or ""
        current = parameter_value(parameter, f"{context} {name}", required=False, allow_missing_static_marker=True)
        if name in {"Volume", "Level"} and current is not None:
            require(math.isclose(current, NEUTRAL_INTERNAL_LEVEL, abs_tol=1.0e-7), f"{context}: {name} nao neutro ({current})")
        if name == "Mute" and current is not None:
            require(math.isclose(current, 0.0, abs_tol=PARAM_ZERO_TOLERANCE), f"{context}: mute ativo")


def audit_bus_chain(chain: ET.Element, objects: Mapping[str, ET.Element], context: str) -> None:
    refs = indexed_refs(chain.findall("./ComponentChain/Components/Component"), "ObjectRef")
    for value in refs:
        component = resolve(objects, value, context)
        if component.tag == "AudioFilterComponent":
            match = component_match(component)
            raise GuardError(f"{context}: efeito de faixa/master ativo {FILTER_LABELS.get(match, match or 'desconhecido')}")
        require(component.tag in {"AudioFader", "AudioMeter"}, f"{context}: componente inesperado {component.tag}")
        if component.tag == "AudioFader":
            audit_fader(component, objects, context)


def audit_panner(panner: ET.Element, objects: Mapping[str, ET.Element], context: str) -> None:
    require(panner.tag in {"StereoToStereoPanProcessor", "DefaultPanProcessor"}, f"{context}: panner inesperado {panner.tag}")
    refs = indexed_refs(panner.findall("./PanProcessor/AudioComponent/Component/Params/Param"), "ObjectRef")
    for value in refs:
        parameter = resolve(objects, value, f"{context} parametro")
        name = parameter.findtext("Name") or ""
        current = parameter_value(parameter, f"{context} {name}", required=False, allow_missing_static_marker=True)
        if name in {"Balance", "Pan"} and current is not None:
            require(math.isclose(current, 0.5, abs_tol=1.0e-7), f"{context}: {name} fora do centro ({current})")


def audit_track_and_master_state(
    sequence: ET.Element,
    objects: Mapping[str, ET.Element],
    target_item_ids: set[str],
) -> None:
    audio_groups: list[ET.Element] = []
    for entry in sequence.findall("./TrackGroups/TrackGroup"):
        second = entry.find("Second")
        group = resolve(objects, second.get("ObjectRef") if second is not None else None, "TrackGroup")
        if group.tag == "AudioTrackGroup":
            audio_groups.append(group)
    require(len(audio_groups) == 1, f"esperado um AudioTrackGroup; encontrado {len(audio_groups)}")
    group = audio_groups[0]

    master_ref = group.find("MasterTrack")
    master = resolve(objects, master_ref.get("ObjectRef") if master_ref is not None else None, "master de audio")
    owner = master.find("./AudioTrack/ComponentOwner/Components")
    master_chain = resolve(objects, owner.get("ObjectRef") if owner is not None else None, "cadeia do master")
    audit_bus_chain(master_chain, objects, "cadeia do master")
    panner_ref = master.find("./AudioTrack/Panner")
    if panner_ref is not None:
        audit_panner(resolve(objects, panner_ref.get("ObjectRef"), "panner do master"), objects, "panner do master")

    track_refs = indexed_refs(group.findall("./TrackGroup/Tracks/Track"), "ObjectURef")
    nonempty: list[set[str]] = []
    for position, value in enumerate(track_refs, 1):
        track = resolve(objects, value, f"faixa A{position}")
        require(track.tag == "AudioClipTrack", f"A{position}: objeto e {track.tag}")
        owner = track.find("./AudioTrack/ComponentOwner/Components")
        chain = resolve(objects, owner.get("ObjectRef") if owner is not None else None, f"cadeia A{position}")
        audit_bus_chain(chain, objects, f"cadeia A{position}")
        panner_ref = track.find("./AudioTrack/Panner")
        if panner_ref is not None:
            audit_panner(resolve(objects, panner_ref.get("ObjectRef"), f"panner A{position}"), objects, f"panner A{position}")
        transitions = track.findall("./ClipTrack/TransitionItems/TrackItems/TrackItem")
        require(not transitions, f"A{position}: {len(transitions)} transicao(oes) de audio")
        refs = set(indexed_refs(track.findall("./ClipTrack/ClipItems/TrackItems/TrackItem"), "ObjectRef"))
        if refs:
            nonempty.append(refs)
    require(len(nonempty) == 1, f"esperada uma faixa de audio nao vazia; encontrado {len(nonempty)}")
    require(nonempty[0] == target_item_ids, "itens da faixa ativa divergem dos 20 alvos")


def audit_master_clip(subclip: ET.Element, objects: Mapping[str, ET.Element], context: str) -> None:
    master_ref = subclip.find("MasterClip")
    master = resolve(objects, master_ref.get("ObjectURef") if master_ref is not None else None, f"{context} MasterClip")
    for entry in master.findall("./AudioComponentChains/AudioComponentChain"):
        chain = resolve(objects, entry.get("ObjectRef"), f"{context} cadeia MasterClip")
        refs = indexed_refs(chain.findall("./ComponentChain/Components/Component"), "ObjectRef")
        components = [resolve(objects, value, f"{context} componente MasterClip") for value in refs]
        matches = [component_match(component) for component in components]
        require(matches in ([], [INTERNAL_VOLUME, INTERNAL_CHANNEL_VOLUME]), f"{context}: MasterClip contem efeitos {matches}")
        for component, match in zip(components, matches):
            audit_intrinsic(component, objects, match, f"{context} MasterClip {match}")


def validate_inactive_metadata(props: Mapping[str, str], context: str) -> None:
    require(not property_is_enabled(props, "EnhanceSpeechCompleted"), f"{context}: EnhanceSpeechCompleted ativo")
    require(not property_is_enabled(props, "ESP.EnhanceSpeechSection.1"), f"{context}: Enhance Speech habilitado")
    require("ESP.EnhanceSpeechSection.EnhanceSpeechEnabled.ComponentID" not in props, f"{context}: ComponentID de Enhance presente")
    require(not property_is_enabled(props, "ESP.ClaritySection.1"), f"{context}: Clarity habilitado")
    require(not property_is_enabled(props, "ESP.SoundEffectsSection.1"), f"{context}: Sound Effects/Reverb habilitado")
    require(not property_is_enabled(props, "ESP.VolumeSection.1"), f"{context}: Volume/limiter Essential habilitado")
    # Mapeamento confirmado pela ligacao ComponentID no .prproj e pelos
    # componentes fisicos: .3 DeHum, .5 DeEss/ReduceSibilance, .7 Rumble e,
    # nas versoes que persistem o controle novo, .10 Reverb.
    # Os amounts podem permanecer lembrados,
    # mas os enables proibidos devem estar desligados.
    for key, label in (
        ("ESP.RestorationSection.3", "DeHum"),
        ("ESP.RestorationSection.7", "Reduce Rumble"),
        ("ESP.RestorationSection.10", "Reduce Reverb"),
    ):
        require(not property_is_enabled(props, key), f"{context}: {label} habilitado em {key}")
    allowed_component_keys = {
        "ESP.RestorationSection.ReduceNoise.ComponentID",
        "ESP.RestorationSection.ReduceSibilance.ComponentID",
    }
    for key in props:
        if key.endswith(".ComponentID") and key not in allowed_component_keys:
            raise GuardError(f"{context}: ComponentID Essential proibido {key}")


def audit_automatch(
    target: Target,
    props: Mapping[str, str],
    mode: str,
    content_states: set[str],
) -> dict[str, Any]:
    clip_props = properties(target.clip.find("./Clip/Node/Properties"), f"{target.clip_id} AudioClip.Properties")
    analysis = {key: value for key, value in clip_props.items() if key.startswith("ESP.LoudnessSection.Analysis.")}
    gains = target.clip.findall("Gain")
    if mode == "forbid":
        require(not gains, f"{target.clip_id}: Gain/Auto-Match presente durante calibracao")
        require(not analysis, f"{target.clip_id}: analise Auto-Match presente durante calibracao")
        require(not property_is_enabled(props, "ESP.LoudnessSection.1"), f"{target.clip_id}: Loudness habilitado durante calibracao")
        return {"mode": "forbid", "present": False}

    require(len(gains) == 1 and gains[0].text, f"{target.clip_id}: Gain individual ausente/duplicado")
    require(set(analysis) == LOUDNESS_PROPERTIES, f"{target.clip_id}: campos de analise {sorted(analysis)}")
    require(property_is_enabled(props, "ESP.LoudnessSection.1"), f"{target.clip_id}: LoudnessSection.1 nao habilitado")
    time_in = int(analysis["ESP.LoudnessSection.Analysis.TimeIn"])
    time_out = int(analysis["ESP.LoudnessSection.Analysis.TimeOut"])
    clip_in = int(target.clip.findtext("./Clip/InPoint") or "0")
    clip_out = int(target.clip.findtext("./Clip/OutPoint") or "-1")
    require(time_in == clip_in and time_out == clip_out, f"{target.clip_id}: janela Auto-Match {time_in}-{time_out}; fonte {clip_in}-{clip_out}")
    state = analysis["ESP.LoudnessSection.Analysis.ContentState"]
    require(state and state not in content_states, f"{target.clip_id}: ContentState ausente/reutilizado {state!r}")
    content_states.add(state)
    loudness = finite_float(analysis["ESP.LoudnessSection.Analysis.ITU1770LoudnessValue"], f"{target.clip_id} loudness analisado")
    gain = finite_float(gains[0].text, f"{target.clip_id} Gain")
    require(gain > 0.0, f"{target.clip_id}: Gain nao positivo")
    expected_gain = 10.0 ** ((-23.0 - loudness) / 20.0)
    require(math.isclose(gain, expected_gain, rel_tol=AUTOMATCH_GAIN_REL_TOLERANCE, abs_tol=AUTOMATCH_GAIN_ABS_TOLERANCE), f"{target.clip_id}: Gain={gain:.12g}; formula -23 exige {expected_gain:.12g}")
    matched = loudness + 20.0 * math.log10(gain)
    require(math.isclose(matched, -23.0, rel_tol=0.0, abs_tol=AUTOMATCH_LUFS_TOLERANCE), f"{target.clip_id}: resultado Auto-Match={matched:.9f} LUFS")
    return {
        "mode": "required",
        "present": True,
        "analysis_lufs": loudness,
        "gain_linear": gain,
        "gain_db": 20.0 * math.log10(gain),
        "matched_lufs": matched,
        "content_state": state,
    }


def audit_target(
    target: Target,
    objects: Mapping[str, ET.Element],
    expected_denoise_amount: float,
    denoise_tolerance: float,
    deess_policy: str,
    expected_deess_ui: float | None,
    expected_deess_effect_order: int | None,
    automatch_mode: str,
    content_states: set[str],
) -> dict[str, Any]:
    context = str(target.clip_id)
    props = properties(target.item.find("./ClipTrackItem/TrackItem/Node/Properties"), f"{context} TrackItem.Properties")
    require(props.get("ESP.AdjustmentsModeID") == "dialog", f"{context}: AdjustmentsModeID nao e dialog")
    require(props.get("ESP.Tag") == "dialog", f"{context}: ESP.Tag nao e dialog")
    require(props.get("ESP.ClipMixTypeID") == "1", f"{context}: ClipMixTypeID nao e 1")
    validate_inactive_metadata(props, context)
    for tag in ("PreRenderComponentChain", "PreRenderMix", "PreRenderSourceItem"):
        require(target.item.find(tag) is None, f"{context}: link de Enhance/pre-render presente ({tag})")

    refs = indexed_refs(target.chain.findall("./ComponentChain/Components/Component"), "ObjectRef")
    components = [resolve(objects, value, f"{context} componente") for value in refs]
    matches = [component_match(component) for component in components]
    intrinsic_matches: list[str] = []
    while matches and matches[0] in {INTERNAL_VOLUME, INTERNAL_CHANNEL_VOLUME}:
        intrinsic_matches.append(matches.pop(0))
        component = components.pop(0)
        audit_intrinsic(component, objects, intrinsic_matches[-1], f"{context} {intrinsic_matches[-1]}")
    require(intrinsic_matches in ([], [INTERNAL_VOLUME, INTERNAL_CHANNEL_VOLUME]), f"{context}: intrinsecos incompletos/fora de ordem {intrinsic_matches}")
    require(matches in ([DENOISE_MATCH], [DENOISE_MATCH, DEESS_MATCH]), f"{context}: cadeia proibida {[FILTER_LABELS.get(value, value or 'desconhecido') for value in matches]}")

    denoise = components[0]
    denoise_values = audit_denoise(denoise, objects, expected_denoise_amount, denoise_tolerance, f"{context} DeNoise")
    require(props.get("ESP.RestorationSection.ReduceNoise.ComponentID") == component_inner_id(denoise, f"{context} DeNoise"), f"{context}: linkage ComponentID do Reduce Noise divergente")
    require(property_is_enabled(props, "ESP.RestorationSection.1"), f"{context}: Reduce Noise nao habilitado")
    ui_noise = finite_float(props.get("ESP.RestorationSection.2"), f"{context} UI Reduce Noise")
    require(math.isclose(ui_noise, denoise_values["Amount"] * 10.0, rel_tol=0.0, abs_tol=max(1.0e-5, denoise_tolerance * 10.0)), f"{context}: UI Reduce Noise={ui_noise}; fisico={denoise_values['Amount'] * 10.0}")

    deess_present = len(matches) == 2
    require(not (deess_policy == "off" and deess_present), f"{context}: DeEss presente, mas politica e off")
    require(not (deess_policy == "required" and not deess_present), f"{context}: DeEss Essential obrigatorio ausente")
    deess_values: dict[str, float | None] | None = None
    deess_order: int | None = None
    deess_ui: float | None = None
    if deess_present:
        deess = components[1]
        deess_values, deess_order = audit_deess(deess, objects, expected_deess_ui, expected_deess_effect_order, f"{context} DeEss Essential")
        require(props.get("ESP.RestorationSection.ReduceSibilance.ComponentID") == component_inner_id(deess, f"{context} DeEss"), f"{context}: DeEss nao esta ligado como Essential ReduceSibilance")
        require(property_is_enabled(props, "ESP.RestorationSection.5"), f"{context}: EnableReduceSibilance (.5) nao habilitado")
        deess_ui = finite_float(props.get("ESP.RestorationSection.6"), f"{context} UI DeEss")
        if expected_deess_ui is not None:
            require(math.isclose(deess_ui, expected_deess_ui, rel_tol=0.0, abs_tol=1.0e-5), f"{context}: UI DeEss={deess_ui}; esperado {expected_deess_ui}")
    else:
        require("ESP.RestorationSection.ReduceSibilance.ComponentID" not in props, f"{context}: ComponentID DeEss orfao")
        require(not property_is_enabled(props, "ESP.RestorationSection.5"), f"{context}: DeEss habilitado sem componente")

    audit_master_clip(target.subclip, objects, context)
    automatch = audit_automatch(target, props, automatch_mode, content_states)
    return {
        **target.row,
        "chain": intrinsic_matches + matches,
        "denoise": {
            "component_object_id": denoise.get("ObjectID"),
            "component_id": component_inner_id(denoise, f"{context} DeNoise"),
            "effect_order": DENOISE_EFFECT_ORDER,
            "ui_amount": ui_noise,
            "amount": denoise_values["Amount"],
            "make_up_gain": denoise_values["Make-Up Gain"],
            "auto_gain": denoise_values["Auto Gain"],
            "focus_shelve_type": denoise_values["Shelve Type"],
            "difference": denoise_values["Output Difference Signal"],
        },
        "deess": {
            "present": deess_present,
            "component_object_id": components[1].get("ObjectID") if deess_present else None,
            "ui_amount": deess_ui,
            "effect_order": deess_order,
            "values": deess_values,
        },
        "automatch": automatch,
    }


def enforce_uniformity(
    rows: Sequence[Mapping[str, Any]],
    deess_policy: str,
    denoise_tolerance: float,
    automatch_mode: str,
) -> dict[str, Any]:
    require(len(rows) == 20, f"uniformidade recebeu {len(rows)} linhas validas; esperado 20")
    denoise_amounts = [float(row["denoise"]["amount"]) for row in rows]
    denoise_objects = {row["denoise"]["component_object_id"] for row in rows}
    require(None not in denoise_objects and len(denoise_objects) == 20, f"DeNoise nao possui 20 instancias independentes: {len(denoise_objects)}")
    require(max(denoise_amounts) - min(denoise_amounts) <= denoise_tolerance, f"DeNoise nao uniforme: min={min(denoise_amounts)}, max={max(denoise_amounts)}")
    deess_rows = [row for row in rows if row["deess"]["present"]]
    if deess_policy == "off":
        require(not deess_rows, "DeEss deveria estar ausente")
    elif deess_policy == "required":
        require(len(deess_rows) == 20, f"DeEss presente em {len(deess_rows)}/20")
    else:
        require(len(deess_rows) in {0, 20}, f"DeEss opcional parcial: {len(deess_rows)}/20")

    deess_order: int | None = None
    deess_ui: float | None = None
    if deess_rows:
        deess_objects = {row["deess"]["component_object_id"] for row in deess_rows}
        require(None not in deess_objects and len(deess_objects) == 20, f"DeEss nao possui 20 instancias independentes: {len(deess_objects)}")
        orders = {int(row["deess"]["effect_order"]) for row in deess_rows}
        require(len(orders) == 1, f"EffectOrder DeEss nao uniforme: {sorted(orders)}")
        deess_order = next(iter(orders))
        ui_values = [float(row["deess"]["ui_amount"]) for row in deess_rows]
        require(max(ui_values) - min(ui_values) <= 1.0e-5, f"UI DeEss nao uniforme: min={min(ui_values)}, max={max(ui_values)}")
        deess_ui = ui_values[0]
        baseline = deess_rows[0]["deess"]["values"]
        for row in deess_rows[1:]:
            current = row["deess"]["values"]
            for name in DEESS_NUMERIC_NAMES:
                require(math.isclose(float(current[name]), float(baseline[name]), rel_tol=0.0, abs_tol=5.0e-7), f"DeEss {name} nao uniforme no ID {row['id']}")
            require(current["Mode"] == baseline["Mode"], f"DeEss Mode nao uniforme no ID {row['id']}")

    if automatch_mode == "required":
        states = {row["automatch"]["content_state"] for row in rows}
        require(len(states) == 20, f"Auto-Match nao possui 20 ContentStates individuais: {len(states)}")
    return {
        "denoise_amount_min": min(denoise_amounts),
        "denoise_amount_max": max(denoise_amounts),
        "denoise_uniform": True,
        "deess_present_count": len(deess_rows),
        "deess_ui_amount": deess_ui,
        "deess_effect_order": deess_order,
        "automatch_individual_count": 20 if automatch_mode == "required" else 0,
    }


def audit(args: argparse.Namespace) -> dict[str, Any]:
    errors: list[str] = []
    rows: list[dict[str, Any]] = []
    project_before: Snapshot | None = None
    project_path: Path | None = None
    summary: dict[str, Any] = {}
    try:
        manifest = load_manifest()
        root, objects, project_before, project_path = load_project(args.project)
        sequence, targets = validate_sources_and_timeline(root, objects, manifest, args.sequence)
        audit_track_and_master_state(sequence, objects, {target.item.get("ObjectID") or "" for target in targets})
        content_states: set[str] = set()
        for target in targets:
            try:
                rows.append(
                    audit_target(
                        target,
                        objects,
                        args.expected_denoise_amount,
                        args.denoise_tolerance,
                        args.deess,
                        args.expected_deess_ui,
                        args.expected_deess_effect_order,
                        args.automatch,
                        content_states,
                    )
                )
            except (GuardError, OSError, ValueError, TypeError, KeyError) as error:
                errors.append(f"{target.clip_id}: erro estrutural: {error}" if not isinstance(error, GuardError) else str(error))
        if not errors:
            summary = enforce_uniformity(rows, args.deess, args.denoise_tolerance, args.automatch)
    except (GuardError, OSError, ValueError, TypeError, KeyError) as error:
        errors.append(f"erro estrutural: {error}" if not isinstance(error, GuardError) else str(error))
    finally:
        if project_before is not None and project_path is not None and project_path.is_file():
            try:
                project_after = snapshot(project_path)
                if project_after != project_before:
                    errors.append("o .prproj mudou durante a auditoria; resultado invalidado")
            except OSError as error:
                errors.append(f"nao foi possivel reverificar o .prproj ao final: {error}")

    return {
        "schema": "codex.premiere.readonly-audit.sem-enhance-essential-20.v1",
        "status": "PASS" if not errors else "FAIL",
        "project": {
            "path": str(project_path or args.project),
            "sha256": project_before.sha256 if project_before else None,
            "size": project_before.size if project_before else None,
            "unchanged_during_audit": not any("mudou durante" in error for error in errors),
        },
        "contract": {
            "sequence": args.sequence,
            "ids": list(EXPECTED_IDS),
            "total_frames": EXPECTED_TOTAL_FRAMES,
            "manifest": str(MANIFEST),
            "manifest_sha256": MANIFEST_SHA256,
            "expected_chain": "optional neutral intrinsics -> DeNoise -> optional Essential DeEss",
            "expected_denoise_amount": args.expected_denoise_amount,
            "denoise_tolerance": args.denoise_tolerance,
            "denoise_other_parameters": {
                "Make-Up Gain": 0.0,
                "Auto Gain": 0.0,
                "Shelve Type (Focus)": 0.0,
                "Output Difference Signal": 0.0,
            },
            "deess_policy": args.deess,
            "expected_deess_ui": args.expected_deess_ui,
            "expected_deess_effect_order": args.expected_deess_effect_order,
            "automatch": args.automatch,
            "automatch_target_lufs": -23.0 if args.automatch == "required" else None,
            "forbidden": ["Enhance", "Dynamics/Clarity", "Reduce Rumble", "DeHum", "Reduce Reverb", "EQ", "limiter", "track/master effects"],
        },
        "summary": summary,
        "items": rows,
        "errors": errors,
    }


def render_text(report: Mapping[str, Any]) -> str:
    project = report["project"]
    contract = report["contract"]
    lines = [
        f"{report['status']} — auditor SEM Enhance Essential (20 clipes)",
        f"Projeto: {project['path']}",
        f"SHA-256: {project['sha256'] or 'indisponivel'}",
        f"Contrato: DeNoise={contract['expected_denoise_amount']:.12g} +/- {contract['denoise_tolerance']:g}; DeEss={contract['deess_policy']}; Auto-Match={contract['automatch']}",
    ]
    summary = report.get("summary") or {}
    if summary:
        lines.append(
            "Resumo: "
            f"20/20 fontes e timings; DeNoise {summary['denoise_amount_min']:.12g}..{summary['denoise_amount_max']:.12g}; "
            f"DeEss {summary['deess_present_count']}/20; Auto-Match individual {summary['automatch_individual_count']}/20"
        )
    errors = report.get("errors") or []
    if errors:
        lines.extend(["", f"Falhas ({len(errors)}):"])
        lines.extend(f"- {error}" for error in errors)
    rows = report.get("items") or []
    if rows:
        lines.extend(["", "ID  DeNoise       MU  AG  Focus  Diff  DeEss  AutoMatch"])
        for row in rows:
            denoise = row["denoise"]
            deess = row["deess"]
            automatch = row["automatch"]
            lines.append(
                f"{row['id']:>3} {denoise['amount']:>12.9f} "
                f"{denoise['make_up_gain']:>3.0f} {denoise['auto_gain']:>3.0f} "
                f"{denoise['focus_shelve_type']:>6.0f} {denoise['difference']:>5.0f} "
                f"{'ON' if deess['present'] else 'OFF':>5} "
                f"{automatch['matched_lufs'] if automatch.get('present') else 'OFF'}"
            )
    return "\n".join(lines) + "\n"


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True, help=".prproj descartavel a auditar")
    parser.add_argument("--sequence", default=SEQUENCE_NAME, help=f"nome exato; padrao: {SEQUENCE_NAME}")
    parser.add_argument("--expected-denoise-amount", type=float, default=DEFAULT_DENOISE_AMOUNT, help=f"Amount fisico 0..1; padrao do primeiro round-trip: {DEFAULT_DENOISE_AMOUNT}")
    parser.add_argument("--denoise-tolerance", type=float, default=DEFAULT_AMOUNT_TOLERANCE)
    parser.add_argument("--deess", choices=("off", "optional", "required"), default="optional", help="off no primeiro round; optional aceita 0/20 ou 20/20; required exige 20/20")
    parser.add_argument("--expected-deess-ui", type=float, help="opcional: valor UI 0..10 e vetor Adobe correspondente")
    parser.add_argument("--expected-deess-effect-order", type=int, help="opcional: EffectOrder exato depois de observado no primeiro save")
    parser.add_argument("--automatch", choices=("forbid", "required"), default="forbid", help="forbid durante calibracao; required valida formula individual exata para -23 LUFS")
    parser.add_argument("--json", action="store_true", help="imprime o relatorio JSON em stdout")
    args = parser.parse_args(argv)
    if not math.isfinite(args.expected_denoise_amount) or not 0.0 <= args.expected_denoise_amount <= 1.0:
        parser.error("--expected-denoise-amount deve estar entre 0 e 1")
    if not math.isfinite(args.denoise_tolerance) or args.denoise_tolerance <= 0.0:
        parser.error("--denoise-tolerance deve ser positivo")
    if args.expected_deess_ui is not None:
        if not math.isfinite(args.expected_deess_ui) or not 0.0 <= args.expected_deess_ui <= 10.0:
            parser.error("--expected-deess-ui deve estar entre 0 e 10")
        if args.deess == "off":
            parser.error("--expected-deess-ui nao pode ser usado com --deess off")
    if args.expected_deess_effect_order is not None and args.deess == "off":
        parser.error("--expected-deess-effect-order nao pode ser usado com --deess off")
    return args


def main(argv: Sequence[str] | None = None) -> int:
    args = parse_args(argv)
    report = audit(args)
    if args.json:
        print(json.dumps(report, ensure_ascii=False, indent=2))
    else:
        print(render_text(report), end="")
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())

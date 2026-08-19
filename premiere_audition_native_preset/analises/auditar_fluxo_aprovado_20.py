#!/usr/bin/env python3
"""Auditoria fail-closed do lote Premiere com o fluxo DJI aprovado.

O programa apenas le o .prproj gzip/XML e, opcionalmente, o WAV concatenado.
Ele nao abre o Premiere, nao altera o projeto e nao processa audio.
"""

from __future__ import annotations

import argparse
import base64
import gzip
import hashlib
import json
import math
import os
import re
import struct
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable


WORKSPACE_ROOT = Path("/Users/lucas/Documents/Codex/voz-neural").resolve()
SAFE_PROJECT_ROOT = (
    WORKSPACE_ROOT
    / "premiere_audition_native_preset"
    / "projetos_descartaveis"
).resolve()
SAFE_MEDIA_ROOT = (
    WORKSPACE_ROOT
    / "premiere_audition_native_preset"
    / "comparacao_ab"
    / "FLUXO_APROVADO_20_20260814"
    / "raws_ordered"
).resolve()
EXPECTED_MANIFEST = SAFE_MEDIA_ROOT.parent / "manifest_preflight.json"

EXPECTED_IDS = [64, 65, 66, 67, 68, 69, 71, 72, 73, 74, 75, 76, 81, 82, 84, 92, 94, 95, 101, 107]
EXCLUDED_IDS = {21, 38, 39, 51, 70, 77, 83}
EXPECTED_TOTAL_FRAMES = 25_181_516

PREMIERE_TICKS_PER_SECOND = 254_016_000_000
SAMPLE_RATE = 48_000
TICKS_PER_SAMPLE = PREMIERE_TICKS_PER_SECOND // SAMPLE_RATE

ENHANCE_MATCH = "7f446972-835b-422e-a8a3-d64028839ba2"
SHELF_MATCH = "8edcd326-33de-4a4d-985b-f1bf48ed1427"
LIMITER_MATCH = "e0b23f05-f1a7-4ef7-9b50-7ec3e3002058"
INTERNAL_VOLUME = "Internal Volume Stereo"
INTERNAL_CHANNEL_VOLUME = "Internal Channel Volume Stereo"

SHELF_OPAQUE_SHA256 = "82c69261dc7cbe8fca91576092fb25da579c85af90ced90806df921cc2402c9b"
LIMITER_OPAQUE_SHA256 = "0fc35130aad50057f92167a9184ff6844bcc3fa9203c221b9d8b73cdce830503"
ENHANCE_E100_OPAQUE_SHA256 = "9c9009b4049b241e41ef7f924c18f5c8a9f1be6e8cc13c432d97cdcf46b7a892"
SHELF_BINARY_HASH = "0cc2d284-c3bf-894f-1a1e-f48b00000525"
LIMITER_BINARY_HASH = "35648616-e39d-c409-c145-b017000001d3"
ENHANCE_E100_BINARY_HASH = "a3bd33c6-789d-b32b-d24a-7bae0000015b"

STEREO_LAYOUT = [{"channellabel": 100}, {"channellabel": 101}]
ID_TOKEN = re.compile(r"(?<!\d)(\d{2,3})(?!\d)")


def sha256_path(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for block in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def under(path: Path, root: Path) -> bool:
    try:
        path.resolve(strict=False).relative_to(root.resolve(strict=False))
        return True
    except ValueError:
        return False


def text(elem: ET.Element | None, path: str, default: str | None = None) -> str | None:
    if elem is None:
        return default
    value = elem.findtext(path)
    return default if value is None else value


def int_text(elem: ET.Element | None, path: str, default: int | None = None) -> int | None:
    value = text(elem, path)
    if value is None:
        return default
    return int(value)


def float_text(elem: ET.Element | None, path: str, default: float | None = None) -> float | None:
    value = text(elem, path)
    if value is None:
        return default
    return float(value)


def properties(elem: ET.Element | None) -> dict[str, str]:
    if elem is None:
        return {}
    return {child.tag: child.text or "" for child in list(elem)}


def indexed_refs(elems: Iterable[ET.Element], attr: str) -> list[str]:
    pairs: list[tuple[int, str]] = []
    for position, elem in enumerate(elems):
        ref = elem.get(attr)
        if ref is None:
            continue
        pairs.append((int(elem.get("Index", position)), ref))
    pairs.sort()
    return [ref for _, ref in pairs]


@dataclass
class FileSnapshot:
    size: int
    mtime_ns: int
    sha256: str


def snapshot(path: Path) -> FileSnapshot:
    st = path.stat()
    return FileSnapshot(st.st_size, st.st_mtime_ns, sha256_path(path))


class Auditor:
    def __init__(
        self,
        project: Path,
        sequence_name: str,
        allowed_media_root: Path,
        manifest: Path | None,
        concat: Path | None,
        require_preview_files: bool,
    ) -> None:
        self.project = project.resolve()
        self.sequence_name = sequence_name
        self.allowed_media_root = allowed_media_root.resolve()
        self.manifest_path = manifest.resolve() if manifest else None
        self.concat = concat.resolve() if concat else None
        self.require_preview_files = require_preview_files
        self.errors: list[str] = []
        self.warnings: list[str] = []
        self.rows: list[dict[str, Any]] = []
        self.report: dict[str, Any] = {}
        self.objects: dict[str, ET.Element] = {}
        self.uobjects: dict[str, ET.Element] = {}
        self.root: ET.Element | None = None
        self.sequence: ET.Element | None = None
        self.manifest_items: dict[int, dict[str, Any]] = {}

    def fail(self, message: str) -> None:
        if message not in self.errors:
            self.errors.append(message)

    def warn(self, message: str) -> None:
        if message not in self.warnings:
            self.warnings.append(message)

    def resolve(self, ref: str | None, context: str) -> ET.Element | None:
        if not ref:
            self.fail(f"{context}: ObjectRef ausente")
            return None
        obj = self.objects.get(ref)
        if obj is None:
            self.fail(f"{context}: ObjectRef {ref} orfao")
        return obj

    def uresolve(self, ref: str | None, context: str) -> ET.Element | None:
        if not ref:
            self.fail(f"{context}: ObjectURef ausente")
            return None
        obj = self.uobjects.get(ref)
        if obj is None:
            self.fail(f"{context}: ObjectURef {ref} orfao")
        return obj

    def load_manifest(self) -> None:
        if self.manifest_path is None:
            self.fail("manifesto preflight obrigatorio para a prova dos 20 clipes")
            return
        if self.allowed_media_root != SAFE_MEDIA_ROOT:
            self.fail(
                f"raiz de midia permitida={self.allowed_media_root}; "
                f"esperado exatamente {SAFE_MEDIA_ROOT}"
            )
        if self.manifest_path != EXPECTED_MANIFEST:
            self.fail(
                f"manifesto={self.manifest_path}; esperado exatamente {EXPECTED_MANIFEST}"
            )
            return
        try:
            payload = json.loads(self.manifest_path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            self.fail(f"manifesto invalido: {exc}")
            return
        raw_items = payload.get("items") or payload.get("pairs") or payload.get("clips")
        if not isinstance(raw_items, list):
            self.fail("manifesto deve conter lista items, pairs ou clips")
            return
        seen: list[int] = []
        raw_details = payload.get("raws") if isinstance(payload.get("raws"), dict) else {}
        for raw in raw_items:
            if not isinstance(raw, dict) or "id" not in raw:
                self.fail("item de manifesto sem id")
                continue
            clip_id = int(raw["id"])
            if clip_id in self.manifest_items:
                self.fail(f"manifesto repete o ID {clip_id}")
                continue
            detail = raw_details.get(str(clip_id), {})
            merged = {**detail, **raw} if isinstance(detail, dict) else dict(raw)
            self.manifest_items[clip_id] = merged
            seen.append(clip_id)
            if merged.get("sample_rate") not in {None, SAMPLE_RATE}:
                self.fail(f"manifesto {clip_id}: sample_rate={merged.get('sample_rate')}")
            if merged.get("channels") not in {None, 2}:
                self.fail(f"manifesto {clip_id}: channels={merged.get('channels')}")
        if seen != EXPECTED_IDS:
            self.fail(f"ordem/IDs do manifesto divergentes: {seen}; esperado {EXPECTED_IDS}")
        fixed_order = [int(value) for value in payload.get("fixed_order", [])]
        if fixed_order and fixed_order != EXPECTED_IDS:
            self.fail(f"fixed_order do manifesto divergente: {fixed_order}")
        if payload.get("preflight_status") not in {None, "PASS_20_OF_20"}:
            self.fail(f"preflight_status do manifesto={payload.get('preflight_status')!r}")
        declared_total = payload.get("raws_total_frames")
        computed_total = sum(
            int(
                next(
                    (
                        raw.get(key)
                        for key in ("raw_frames", "frames", "source_frames", "num_frames")
                        if raw.get(key) is not None
                    ),
                    0,
                )
            )
            for raw in raw_items
            if isinstance(raw, dict)
        )
        if declared_total is not None and int(declared_total) != computed_total:
            self.fail(
                f"raws_total_frames declarado={declared_total}; soma dos pares={computed_total}"
            )
        if computed_total != EXPECTED_TOTAL_FRAMES:
            self.fail(
                f"soma fixa dos 20 raws={computed_total}; esperado {EXPECTED_TOTAL_FRAMES}"
            )

    def load_project(self) -> FileSnapshot | None:
        if not under(self.project, SAFE_PROJECT_ROOT):
            self.fail(f"projeto fora da area descartavel segura: {self.project}")
            return None
        if not self.project.is_file():
            self.fail(f"projeto inexistente: {self.project}")
            return None
        before = snapshot(self.project)
        try:
            with self.project.open("rb") as fh:
                if fh.read(2) != b"\x1f\x8b":
                    raise ValueError("nao possui assinatura gzip")
            with gzip.open(self.project, "rb") as fh:
                xml_bytes = fh.read()
            self.root = ET.fromstring(xml_bytes)
        except Exception as exc:  # noqa: BLE001
            self.fail(f"gzip/XML invalido: {exc}")
            return before
        if self.root.tag != "PremiereData":
            self.fail(f"raiz XML inesperada: {self.root.tag}")

        for elem in list(self.root):
            oid = elem.get("ObjectID")
            if oid:
                if oid in self.objects:
                    self.fail(f"ObjectID direto duplicado: {oid}")
                self.objects[oid] = elem
            uid = elem.get("ObjectUID")
            if uid:
                if uid in self.uobjects:
                    self.fail(f"ObjectUID direto duplicado: {uid}")
                self.uobjects[uid] = elem

        sequences = [
            elem
            for elem in self.root.findall("Sequence")
            if text(elem, "Name") == self.sequence_name
        ]
        if len(sequences) != 1:
            names = [text(elem, "Name", "") for elem in self.root.findall("Sequence")]
            self.fail(
                f"sequencia exata {self.sequence_name!r}: encontradas {len(sequences)}; "
                f"disponiveis={names}"
            )
        else:
            self.sequence = sequences[0]

        self.report["project"] = {
            "path": str(self.project),
            "size": before.size,
            "mtime_ns": before.mtime_ns,
            "sha256": before.sha256,
            "gzip_xml_valid": not self.errors,
        }
        return before

    def audit_all_project_media(self) -> None:
        """Inventaria midia nao alcancavel sem confundi-la com o render ativo.

        Projetos descartaveis antigos podem conservar bins/sequencias de apoio. A
        prova de pureza e feita, de forma estrita, nas 20 fontes alcancaveis pela
        sequencia escolhida. Referencias absolutas externas mas nao alcancaveis
        ficam registradas como aviso; nunca sao abertas por esta auditoria.
        """
        assert self.root is not None
        for media in self.root.findall("Media"):
            candidates = [
                value
                for value in (text(media, "ActualMediaFilePath"), text(media, "FilePath"))
                if value and value.startswith("/")
            ]
            title = text(media, "Title", "") or ""
            for candidate in candidates:
                path = Path(candidate).resolve(strict=False)
                if not under(path, WORKSPACE_ROOT):
                    self.warn(f"Media nao alcancavel pode apontar fora do workspace: {path}")
                tokens = {int(v) for v in ID_TOKEN.findall(path.name + " " + title)}
                bad = sorted(tokens & EXCLUDED_IDS)
                if bad:
                    self.fail(f"Media do projeto contem ID excluido {bad}: {path}")

    def audio_tracks(self) -> list[ET.Element]:
        assert self.sequence is not None
        groups: list[ET.Element] = []
        for entry in self.sequence.findall("./TrackGroups/TrackGroup"):
            second = entry.find("Second")
            group = self.resolve(second.get("ObjectRef") if second is not None else None, "TrackGroup")
            if group is not None and group.tag == "AudioTrackGroup":
                groups.append(group)
        if len(groups) != 1:
            self.fail(f"sequencia deve ter exatamente um AudioTrackGroup; encontrado {len(groups)}")
            return []
        group = groups[0]

        master = self.resolve(
            group.find("MasterTrack").get("ObjectRef") if group.find("MasterTrack") is not None else None,
            "master de audio",
        )
        if master is not None:
            owner = master.find("./AudioTrack/ComponentOwner/Components")
            self.audit_non_item_chain(
                owner.get("ObjectRef") if owner is not None else None,
                "cadeia do master",
            )
            panner = master.find("./AudioTrack/Panner")
            self.audit_panner(
                panner.get("ObjectRef") if panner is not None else None,
                "panner do master",
            )

        refs = indexed_refs(group.findall("./TrackGroup/Tracks/Track"), "ObjectURef")
        tracks: list[ET.Element] = []
        for index, ref in enumerate(refs):
            track = self.uresolve(ref, f"faixa de audio {index}")
            if track is None:
                continue
            if track.tag != "AudioClipTrack":
                self.fail(f"faixa {index} nao e AudioClipTrack: {track.tag}")
                continue
            owner = track.find("./AudioTrack/ComponentOwner/Components")
            self.audit_non_item_chain(
                owner.get("ObjectRef") if owner is not None else None,
                f"cadeia da faixa A{index + 1}",
            )
            panner = track.find("./AudioTrack/Panner")
            self.audit_panner(
                panner.get("ObjectRef") if panner is not None else None,
                f"panner da faixa A{index + 1}",
            )
            transitions = track.findall("./ClipTrack/TransitionItems/TrackItems/TrackItem")
            if transitions:
                self.fail(f"A{index + 1} possui {len(transitions)} transicao(oes) de audio")
            tracks.append(track)
        return tracks

    def audit_non_item_chain(self, chain_ref: str | None, context: str) -> None:
        chain = self.resolve(chain_ref, context)
        if chain is None:
            return
        refs = indexed_refs(chain.findall("./ComponentChain/Components/Component"), "ObjectRef")
        for ref in refs:
            component = self.resolve(ref, context)
            if component is None:
                continue
            if component.tag == "AudioFilterComponent":
                self.fail(
                    f"{context} contem AudioFilterComponent ativo "
                    f"({text(component, 'DisplayName') or text(component, 'FilterMatchName')})"
                )
            elif component.tag not in {"AudioFader", "AudioMeter"}:
                self.fail(f"{context} contem componente inesperado {component.tag}")
            elif component.tag == "AudioFader":
                for param_ref in component.findall("./AudioComponent/Component/Params/Param"):
                    param = self.resolve(param_ref.get("ObjectRef"), context)
                    if param is None:
                        continue
                    name = text(param, "Name", "") or ""
                    if text(param, "IsTimeVarying") == "true" or param.find("Keyframes") is not None:
                        self.fail(f"{context}: fader {name} possui automacao")
                    value = self.param_value(param)
                    if name in {"Volume", "Level"} and value is not None and not math.isclose(
                        value, 0.17782793939113617, rel_tol=0, abs_tol=1e-7
                    ):
                        self.fail(f"{context}: fader {name} nao neutro ({value})")
                    if name == "Mute" and value not in {None, 0.0}:
                        self.fail(f"{context}: mute ativo ({value})")

    def audit_panner(self, panner_ref: str | None, context: str) -> None:
        panner = self.resolve(panner_ref, context)
        if panner is None:
            return
        if panner.tag not in {"StereoToStereoPanProcessor", "DefaultPanProcessor"}:
            self.fail(f"{context}: tipo inesperado {panner.tag}")
            return
        layout_raw = text(panner, "./PanProcessor/AudioComponent/AudioChannelLayout")
        if layout_raw:
            try:
                layout = json.loads(layout_raw)
            except json.JSONDecodeError:
                layout = None
            if layout != STEREO_LAYOUT:
                self.fail(f"{context}: layout nao estereo {layout_raw}")
        for param_ref in panner.findall("./PanProcessor/AudioComponent/Component/Params/Param"):
            param = self.resolve(param_ref.get("ObjectRef"), context)
            if param is None:
                continue
            name = text(param, "Name", "") or ""
            if text(param, "IsTimeVarying") == "true" or param.find("Keyframes") is not None:
                self.fail(f"{context}: {name} possui automacao")
            value = self.param_value(param)
            if name in {"Balance", "Pan"} and value is not None and not math.isclose(
                value, 0.5, rel_tol=0, abs_tol=1e-7
            ):
                self.fail(f"{context}: {name} fora do centro ({value})")

    def component_kind(self, component: ET.Element) -> str:
        match = text(component, "FilterMatchName", "") or ""
        custom = text(component, "CustomDisplayName", "") or ""
        if match == ENHANCE_MATCH or custom == "Enhance Speech":
            return "enhance"
        if match == INTERNAL_VOLUME:
            return "volume"
        if match == INTERNAL_CHANNEL_VOLUME:
            return "channel_volume"
        if match == SHELF_MATCH:
            return "shelf"
        if match == LIMITER_MATCH:
            return "limiter"
        return "unknown"

    def component_params(self, component: ET.Element, context: str) -> dict[str, ET.Element]:
        result: dict[str, ET.Element] = {}
        for ref_elem in component.findall("./AudioComponent/Component/Params/Param"):
            param = self.resolve(ref_elem.get("ObjectRef"), context)
            if param is None:
                continue
            name = text(param, "Name") or f"__index_{ref_elem.get('Index', '?')}"
            if name in result and not name.startswith("__index_"):
                # Parametric Equalizer serializa dois controles Bypass nativos.
                # Ambos sao auditados abaixo; essa duplicacao nao e anomalia.
                if name != "Bypass":
                    self.warn(f"{context}: parametro repetido {name}")
            result[name] = param
            varying = text(param, "IsTimeVarying")
            if varying != "false":
                self.fail(f"{context}: parametro {name} tem IsTimeVarying={varying!r}")
            if param.find("Keyframes") is not None:
                self.fail(f"{context}: parametro {name} possui keyframes")
        return result

    def named_param_values(
        self,
        component: ET.Element,
        name: str,
        context: str,
    ) -> list[float | None]:
        values: list[float | None] = []
        for ref_elem in component.findall("./AudioComponent/Component/Params/Param"):
            param = self.resolve(ref_elem.get("ObjectRef"), context)
            if param is not None and text(param, "Name") == name:
                values.append(self.param_value(param))
        return values

    @staticmethod
    def param_value(param: ET.Element | None) -> float | None:
        if param is None:
            return None
        current = text(param, "CurrentValue")
        if current is not None:
            return float(current)
        start = text(param, "StartKeyframe")
        if start:
            fields = start.split(",")
            if len(fields) >= 2:
                return float(fields[1])
        return None

    def audit_stereo(self, elem: ET.Element, context: str) -> None:
        layout_raw = text(elem, "./AudioComponent/AudioChannelLayout") or text(elem, "AudioChannelLayout")
        if layout_raw is None:
            self.fail(f"{context}: AudioChannelLayout ausente")
        else:
            try:
                layout = json.loads(layout_raw)
            except json.JSONDecodeError:
                layout = None
            if layout != STEREO_LAYOUT:
                self.fail(f"{context}: layout nao estereo {layout_raw}")
        channel_type = text(elem, "./AudioComponent/ChannelType") or text(elem, "ChannelType")
        if channel_type not in {None, "1"}:
            self.fail(f"{context}: ChannelType={channel_type}, esperado 1 (stereo)")
        config_raw = text(elem, "ChannelConfigData")
        if config_raw:
            try:
                config = json.loads(config_raw)
                layouts = [entry.get("layout") for side in ("in", "out") for entry in config.get(side, [])]
                if not layouts or any(layout != [100, 101] for layout in layouts):
                    self.fail(f"{context}: ChannelConfigData nao e integralmente stereo: {config_raw}")
            except Exception as exc:  # noqa: BLE001
                self.fail(f"{context}: ChannelConfigData invalido: {exc}")

    def opaque_sha(self, component: ET.Element, context: str) -> str | None:
        opaque = component.find("OpaqueData")
        if opaque is None:
            self.fail(f"{context}: OpaqueData ausente")
            return None
        # Premiere deduplica payloads iguais: uma instancia guarda os bytes e as
        # demais conservam somente BinaryHash. Resolva essa referencia dentro do
        # proprio projeto; nunca aceite um payload ausente apenas por inferencia.
        candidates = [opaque]
        if not (opaque.text or "").strip():
            binary_hash = opaque.get("BinaryHash")
            if not binary_hash or self.root is None:
                self.fail(f"{context}: payload deduplicado sem BinaryHash resolvivel")
                return None
            candidates = [
                item
                for item in self.root.iter("OpaqueData")
                if item.get("BinaryHash") == binary_hash and (item.text or "").strip()
            ]
            if not candidates:
                self.fail(
                    f"{context}: nenhum payload materializado para BinaryHash={binary_hash}"
                )
                return None

        decoded_hashes: set[str] = set()
        for candidate in candidates:
            if candidate.get("Encoding") != "base64":
                self.fail(f"{context}: OpaqueData nao esta em base64")
                return None
            try:
                # Premiere wraps long base64 payloads across XML lines.
                encoded = "".join((candidate.text or "").split())
                decoded = base64.b64decode(encoded, validate=True)
            except Exception as exc:  # noqa: BLE001
                self.fail(f"{context}: OpaqueData base64 invalido: {exc}")
                return None
            decoded_hashes.add(hashlib.sha256(decoded).hexdigest())
        if len(decoded_hashes) != 1:
            self.fail(
                f"{context}: BinaryHash aponta para payloads divergentes {sorted(decoded_hashes)}"
            )
            return None
        return next(iter(decoded_hashes))

    def audit_enhance(
        self,
        component: ET.Element,
        item: ET.Element,
        item_props: dict[str, str],
        duration: int,
        clip_id: int,
    ) -> tuple[float | None, str | None]:
        context = f"{clip_id} Enhance Speech"
        params = self.component_params(component, context)
        mix = self.param_value(params.get("Mix Amount"))
        opaque = self.opaque_sha(component, context)
        opaque_elem = component.find("OpaqueData")
        binary_hash = opaque_elem.get("BinaryHash") if opaque_elem is not None else None
        if binary_hash != ENHANCE_E100_BINARY_HASH:
            self.fail(
                f"{context}: BinaryHash={binary_hash}; esperado E100 {ENHANCE_E100_BINARY_HASH}"
            )
        if opaque != ENHANCE_E100_OPAQUE_SHA256:
            self.fail(
                f"{context}: SHA do OpaqueData={opaque}; esperado E100 "
                f"{ENHANCE_E100_OPAQUE_SHA256}"
            )
        if mix is not None and not math.isclose(mix, 1.0, abs_tol=1e-7):
            self.fail(f"{context}: Mix Amount={mix}; esperado 1.0 (100%)")
        bypass_values = self.named_param_values(component, "Bypass", context)
        if any(value not in {None, 0.0} for value in bypass_values):
            self.fail(f"{context}: bypass ativo ({bypass_values})")

        required_props = {
            "ESP.Tag": "dialog",
            "ESP.AdjustmentsModeID": "dialog",
            "EnhanceSpeechCompleted": "true",
            "ESP.EnhanceSpeechSection.1": "1",
            "ESP.EnhanceSpeechSection.2": "10",
            "ESP.LoudnessSection.1": "1",
        }
        for key, expected in required_props.items():
            actual = item_props.get(key)
            if actual != expected:
                self.fail(f"{clip_id}: propriedade {key}={actual!r}; esperado {expected!r}")
        optional_numeric_props = {
            "ESP.EnhanceSpeechSection.3": 3.0,
            "ESP.VolumeSection.1": 1.0,
        }
        for key, expected in optional_numeric_props.items():
            if key not in item_props:
                continue
            try:
                actual = float(item_props[key])
            except ValueError:
                self.fail(f"{clip_id}: propriedade {key} invalida: {item_props[key]!r}")
                continue
            if not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-7):
                self.fail(f"{clip_id}: propriedade {key}={actual}; esperado {expected}")

        pre_chain = item.find("PreRenderComponentChain")
        pre_mix = item.find("PreRenderMix")
        pre_source = item.find("PreRenderSourceItem")
        self.resolve(pre_chain.get("ObjectRef") if pre_chain is not None else None, f"{context} pre-render chain")
        enhance_ref = component.get("ObjectID")
        mix_ref = pre_mix.get("ObjectRef") if pre_mix is not None else None
        if mix_ref != enhance_ref:
            self.fail(f"{context}: PreRenderMix={mix_ref}, componente Enhance={enhance_ref}")
        preview = self.resolve(
            pre_source.get("ObjectRef") if pre_source is not None else None,
            f"{context} preview",
        )
        preview_path: str | None = None
        if preview is not None:
            if preview.tag != "AudioPreviewItem":
                self.fail(f"{context}: PreRenderSourceItem nao e AudioPreviewItem ({preview.tag})")
            pstart = int_text(preview, "Start", 0)
            pend = int_text(preview, "End")
            if pstart != 0 or pend != duration:
                self.fail(f"{context}: preview cobre {pstart}-{pend}; esperado 0-{duration}")
            preview_path = text(preview, "FilePath")
            if not preview_path or not preview_path.startswith("/"):
                self.fail(f"{context}: caminho absoluto do preview ausente")
            else:
                pp = Path(preview_path).resolve(strict=False)
                if not under(pp, WORKSPACE_ROOT):
                    self.fail(f"{context}: preview fora do workspace: {pp}")
                elif self.require_preview_files and (not pp.is_file() or pp.stat().st_size <= 0):
                    self.fail(f"{context}: arquivo de preview ausente/vazio: {pp}")
        # Em E100 o Premiere 2026 omite CurrentValue/StartKeyframe porque 1.0 e
        # o default. O OpaqueData decodificado e a propriedade UI=10 sao a prova
        # materializada; em E70 o payload e outro e traz 0.69999999.
        effective_mix = 1.0 if (
            opaque == ENHANCE_E100_OPAQUE_SHA256
            and item_props.get("ESP.EnhanceSpeechSection.2") == "10"
        ) else mix
        return effective_mix, preview_path

    def audit_shelf(self, component: ET.Element, clip_id: int) -> None:
        context = f"{clip_id} shelf 60/-12"
        params = self.component_params(component, context)
        opaque = self.opaque_sha(component, context)
        opaque_elem = component.find("OpaqueData")
        binary_hash = opaque_elem.get("BinaryHash") if opaque_elem is not None else None
        if binary_hash != SHELF_BINARY_HASH:
            self.fail(f"{context}: BinaryHash={binary_hash}; esperado {SHELF_BINARY_HASH}")
        if opaque is not None and opaque != SHELF_OPAQUE_SHA256:
            self.fail(f"{context}: SHA do OpaqueData={opaque}; esperado {SHELF_OPAQUE_SHA256}")
        expected_values = {
            "Low Shelf Frequency": 0.0016680567059665918,
            "Low Shelf Gain": 0.375,
            "Gain": 0.66666698455810547,
            "ConstantQ": 0.0,
            "High Shelf Enable": 0.0,
            "High-Pass Filter Enable": 0.0,
            "Low-Pass Filter Enable": 0.0,
        }
        for name, expected in expected_values.items():
            actual = self.param_value(params.get(name))
            if actual is None and name.endswith("Enable"):
                actual = 0.0
            if actual is None or not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-7):
                self.fail(f"{context}: {name}={actual}; esperado {expected}")
        # Em alguns roundtrips estes tres valores ficam apenas no payload
        # congelado. Se tambem estiverem serializados como CurrentValue, exija
        # coerencia com o payload aprovado.
        low_shelf_enable = self.param_value(params.get("Low Shelf Enable"))
        if low_shelf_enable is not None and not math.isclose(
            low_shelf_enable, 1.0, rel_tol=0, abs_tol=1e-7
        ):
            self.fail(f"{context}: Low Shelf Enable={low_shelf_enable}; esperado 1")
        for band in range(1, 6):
            name = f"EQ Band {band} Enable"
            actual = self.param_value(params.get(name))
            if actual is None:
                actual = 0.0
            if not math.isclose(actual, 0.0, abs_tol=1e-7):
                self.fail(f"{context}: {name}={actual}; esperado 0")
        bypass_values = self.named_param_values(component, "Bypass", context)
        if any(value not in {None, 0.0} for value in bypass_values):
            self.fail(f"{context}: bypass ativo {bypass_values}")

    def audit_limiter(self, component: ET.Element, clip_id: int) -> None:
        context = f"{clip_id} limiter v1"
        params = self.component_params(component, context)
        opaque = self.opaque_sha(component, context)
        if opaque is not None and opaque != LIMITER_OPAQUE_SHA256:
            self.fail(f"{context}: SHA do OpaqueData={opaque}; esperado {LIMITER_OPAQUE_SHA256}")
        binary_hash = component.find("OpaqueData").get("BinaryHash") if component.find("OpaqueData") is not None else None
        if binary_hash != LIMITER_BINARY_HASH:
            self.fail(f"{context}: BinaryHash={binary_hash}; esperado {LIMITER_BINARY_HASH}")
        instance = text(component, "./AudioComponent/Component/InstanceName")
        if instance != "DJI_FEMININA_CAPCUT_BASE__LIMITER_FINAL_v1":
            self.fail(f"{context}: InstanceName={instance!r}")
        expected_values = {
            "Maximum Amplitude": 0.99699997901916504,
            "Input Boost": 0.72066664695739746,
            "Look-Ahead Time": 0.0,
            "Limit True Peak": 0.0,
        }
        for name, expected in expected_values.items():
            actual = self.param_value(params.get(name))
            if actual is None or not math.isclose(actual, expected, rel_tol=0, abs_tol=1e-7):
                self.fail(f"{context}: {name}={actual}; esperado {expected}")
        for name in ("Release Time", "Link Channels"):
            actual = self.param_value(params.get(name))
            if actual is not None and not math.isclose(actual, 1.0, rel_tol=0, abs_tol=1e-7):
                self.fail(f"{context}: {name}={actual}; esperado 1 no payload v1")
        bypass_values = self.named_param_values(component, "Bypass", context)
        if any(value not in {None, 0.0} for value in bypass_values):
            self.fail(f"{context}: bypass ativo ({bypass_values})")

    def audit_intrinsics(self, component: ET.Element, kind: str, clip_id: int) -> None:
        context = f"{clip_id} {kind} intrinseco"
        params = self.component_params(component, context)
        neutral = 0.17782793939113617
        names = ["Level"] if kind == "volume" else ["Left", "Right"]
        for name in names:
            actual = self.param_value(params.get(name))
            if actual is None or not math.isclose(actual, neutral, rel_tol=0, abs_tol=1e-7):
                self.fail(f"{context}: {name}={actual}; esperado volume manual neutro")
        for name in ("Mute", "Bypass"):
            values = self.named_param_values(component, name, context)
            if any(value not in {None, 0.0} for value in values):
                self.fail(f"{context}: {name} ativo ({values})")

    def audit_master_clip(self, subclip: ET.Element, clip_id: int) -> None:
        master_ref = subclip.find("MasterClip")
        master = self.uresolve(
            master_ref.get("ObjectURef") if master_ref is not None else None,
            f"{clip_id} MasterClip",
        )
        if master is None:
            return
        for chain_entry in master.findall("./AudioComponentChains/AudioComponentChain"):
            chain = self.resolve(chain_entry.get("ObjectRef"), f"{clip_id} cadeia do MasterClip")
            if chain is None:
                continue
            for ref in indexed_refs(chain.findall("./ComponentChain/Components/Component"), "ObjectRef"):
                component = self.resolve(ref, f"{clip_id} cadeia do MasterClip")
                if component is None:
                    continue
                kind = self.component_kind(component)
                if component.tag != "AudioFilterComponent" or kind not in {
                    "volume",
                    "channel_volume",
                }:
                    self.fail(
                        f"{clip_id}: MasterClip contem efeito inesperado "
                        f"{text(component, 'FilterMatchName') or component.tag}"
                    )
                    continue
                self.audit_stereo(component, f"{clip_id} MasterClip {kind}")
                self.audit_intrinsics(component, kind, clip_id)

    def media_for_audio_clip(self, audio_clip: ET.Element, clip_id: int) -> tuple[Path | None, ET.Element | None]:
        source = audio_clip.find("./Clip/Source")
        media_source = self.resolve(
            source.get("ObjectRef") if source is not None else None,
            f"{clip_id} AudioMediaSource",
        )
        if media_source is None:
            return None, None
        if media_source.tag != "AudioMediaSource":
            self.fail(f"{clip_id}: fonte nao e AudioMediaSource ({media_source.tag})")
        media_ref = media_source.find("./MediaSource/Media")
        media = self.uresolve(
            media_ref.get("ObjectURef") if media_ref is not None else None,
            f"{clip_id} Media",
        )
        if media is None:
            return None, None
        path_text = text(media, "ActualMediaFilePath") or text(media, "FilePath")
        if not path_text or not path_text.startswith("/"):
            self.fail(f"{clip_id}: Media sem caminho absoluto real")
            return None, media
        path = Path(path_text).resolve(strict=False)
        if not under(path, self.allowed_media_root):
            self.fail(f"{clip_id}: fonte fora da raiz permitida {self.allowed_media_root}: {path}")
        elif not path.is_file():
            self.fail(f"{clip_id}: fonte preparada inexistente: {path}")

        stream_ref = media.find("AudioStream")
        if stream_ref is None:
            stream_ref = media.find("DataStream")
        stream = self.resolve(
            stream_ref.get("ObjectRef") if stream_ref is not None else None,
            f"{clip_id} AudioStream",
        )
        if stream is not None:
            if stream.tag != "AudioStream":
                self.fail(f"{clip_id}: DataStream nao e AudioStream ({stream.tag})")
            self.audit_stereo(stream, f"{clip_id} AudioStream")
            if int_text(stream, "FrameRate") != TICKS_PER_SAMPLE:
                self.fail(
                    f"{clip_id}: FrameRate do AudioStream={int_text(stream, 'FrameRate')}; "
                    f"esperado {TICKS_PER_SAMPLE} (48 kHz)"
                )
        return path, stream

    def manifest_expected(self, clip_id: int) -> tuple[Path | None, int | None, str | None]:
        raw = self.manifest_items.get(clip_id)
        if raw is None:
            return None, None, None
        path_value = next(
            (
                raw.get(key)
                for key in (
                    "source_path",
                    "raw_path",
                    "prepared_source",
                    "prepared_path",
                    "raw_copy",
                    "input",
                    "path",
                )
                if raw.get(key)
            ),
            None,
        )
        frame_value = next(
            (
                raw.get(key)
                for key in (
                    "frames",
                    "raw_frames",
                    "source_frames",
                    "num_frames",
                    "sample_frames",
                )
                if raw.get(key) is not None
            ),
            None,
        )
        if path_value:
            candidate = Path(path_value)
            if not candidate.is_absolute() and self.manifest_path is not None:
                candidate = self.manifest_path.parent / candidate
            expected_path = candidate.resolve(strict=False)
        else:
            expected_path = None
        return (
            expected_path,
            int(frame_value) if frame_value is not None else None,
            str(raw.get("file_sha256")) if raw.get("file_sha256") else None,
        )

    def audit_item(self, item: ET.Element, expected_id: int) -> dict[str, Any]:
        item_oid = item.get("ObjectID", "?")
        track_item = item.find("./ClipTrackItem/TrackItem")
        start = int_text(track_item, "Start", 0)
        end = int_text(track_item, "End")
        if start is None or end is None or end <= start:
            self.fail(f"item {item_oid}: intervalo invalido {start}-{end}")
            start = start or 0
            end = end or start
        duration = end - start
        if duration % TICKS_PER_SAMPLE:
            self.fail(f"{expected_id}: duracao {duration} nao cai em fronteira de amostra 48 kHz")
        frames = duration // TICKS_PER_SAMPLE
        if item.find("./ClipTrackItem/HeadTransition") is not None or item.find("./ClipTrackItem/TailTransition") is not None:
            self.fail(f"{expected_id}: item possui HeadTransition/TailTransition")

        item_props = properties(item.find("./ClipTrackItem/TrackItem/Node/Properties"))
        for key, value in item_props.items():
            lowered = key.lower()
            if ("disabled" in lowered or "mute" in lowered) and value.lower() in {"1", "true"}:
                self.fail(f"{expected_id}: item desativado/mutado por {key}={value}")

        sub_ref = item.find("./ClipTrackItem/SubClip")
        subclip = self.resolve(
            sub_ref.get("ObjectRef") if sub_ref is not None else None,
            f"item {item_oid} SubClip",
        )
        name = text(subclip, "Name", "") or ""
        tokens = {int(value) for value in ID_TOKEN.findall(name)}
        if expected_id not in tokens:
            self.fail(f"item {item_oid}: nome {name!r} nao identifica o esperado {expected_id}")
        identified_authorized = tokens & set(EXPECTED_IDS)
        if identified_authorized != {expected_id}:
            self.fail(
                f"item {item_oid}: IDs autorizados ambiguos no nome {name!r}: "
                f"{sorted(identified_authorized)}"
            )
        bad = sorted(tokens & EXCLUDED_IDS)
        if bad:
            self.fail(f"item {item_oid}: nome contem ID excluido {bad}: {name}")

        audio_clip = None
        media_path = None
        loudness = None
        gain = None
        gain_db = None
        matched_lufs = None
        if subclip is not None:
            clip_ref = subclip.find("Clip")
            audio_clip = self.resolve(
                clip_ref.get("ObjectRef") if clip_ref is not None else None,
                f"{expected_id} AudioClip",
            )
        if audio_clip is not None:
            if audio_clip.tag != "AudioClip":
                self.fail(f"{expected_id}: SubClip aponta para {audio_clip.tag}, nao AudioClip")
            self.audit_stereo(audio_clip, f"{expected_id} AudioClip")
            clip_in = int_text(audio_clip, "./Clip/InPoint", 0)
            clip_out = int_text(audio_clip, "./Clip/OutPoint")
            if clip_in != 0:
                self.fail(f"{expected_id}: fonte nao comeca no frame zero ({clip_in})")
            if clip_out is None or clip_out - (clip_in or 0) != duration:
                self.fail(
                    f"{expected_id}: item dura {duration}, mas usa fonte {clip_in}-{clip_out}"
                )
            clip_props = properties(audio_clip.find("./Clip/Node/Properties"))
            for key in (
                "ESP.LoudnessSection.Analysis.TimeIn",
                "ESP.LoudnessSection.Analysis.TimeOut",
                "ESP.LoudnessSection.Analysis.ContentState",
                "ESP.LoudnessSection.Analysis.ITU1770LoudnessValue",
            ):
                if not clip_props.get(key):
                    self.fail(f"{expected_id}: analise Auto-Match ausente: {key}")
            analysis_in = int(clip_props.get("ESP.LoudnessSection.Analysis.TimeIn", "0") or 0)
            analysis_out = int(clip_props.get("ESP.LoudnessSection.Analysis.TimeOut", "0") or 0)
            if analysis_in != (clip_in or 0) or analysis_out != clip_out:
                self.fail(
                    f"{expected_id}: janela Auto-Match {analysis_in}-{analysis_out}; "
                    f"fonte {clip_in}-{clip_out}"
                )
            try:
                loudness = float(clip_props["ESP.LoudnessSection.Analysis.ITU1770LoudnessValue"])
            except (KeyError, ValueError):
                self.fail(f"{expected_id}: loudness de analise invalido")
            gain = float_text(audio_clip, "Gain", 1.0)
            if gain is None or gain <= 0:
                self.fail(f"{expected_id}: Gain do Auto-Match invalido {gain}")
            elif loudness is not None:
                gain_db = 20.0 * math.log10(gain)
                matched_lufs = loudness + gain_db
                if not math.isclose(matched_lufs, -23.0, rel_tol=0, abs_tol=0.02):
                    self.fail(
                        f"{expected_id}: Auto-Match resulta {matched_lufs:.5f} LUFS "
                        f"(analise {loudness:.5f} + ganho {gain_db:.5f}), nao -23"
                    )
            media_path, _ = self.media_for_audio_clip(audio_clip, expected_id)
            if media_path is not None:
                media_tokens = {int(value) for value in ID_TOKEN.findall(media_path.name)}
                if expected_id not in media_tokens:
                    self.fail(f"{expected_id}: fonte nao identifica o ID: {media_path.name}")
                media_authorized = media_tokens & set(EXPECTED_IDS)
                if media_authorized != {expected_id}:
                    self.fail(
                        f"{expected_id}: IDs autorizados ambiguos na fonte: "
                        f"{sorted(media_authorized)} ({media_path.name})"
                    )
                bad_media = sorted(media_tokens & EXCLUDED_IDS)
                if bad_media:
                    self.fail(f"{expected_id}: fonte contem ID excluido {bad_media}: {media_path}")
                expected_path, expected_frames, expected_sha = self.manifest_expected(expected_id)
                if expected_path is not None and media_path != expected_path:
                    self.fail(
                        f"{expected_id}: fonte no projeto {media_path}; manifesto exige {expected_path}"
                    )
                if expected_frames is not None and frames != expected_frames:
                    self.fail(
                        f"{expected_id}: {frames} frames na timeline; manifesto exige {expected_frames}"
                    )
                if expected_sha is not None:
                    actual_sha = sha256_path(media_path)
                    if actual_sha != expected_sha:
                        self.fail(
                            f"{expected_id}: SHA da fonte={actual_sha}; manifesto exige {expected_sha}"
                        )
            self.audit_master_clip(subclip, expected_id)

        owner = item.find("./ClipTrackItem/ComponentOwner/Components")
        chain = self.resolve(
            owner.get("ObjectRef") if owner is not None else None,
            f"{expected_id} cadeia do item",
        )
        kinds: list[str] = []
        enhance_mix = None
        preview_path = None
        if chain is not None:
            self.audit_stereo(chain, f"{expected_id} AudioComponentChain")
            component_elems = chain.findall("./ComponentChain/Components/Component")
            component_refs = indexed_refs(component_elems, "ObjectRef")
            indices = sorted(int(elem.get("Index", i)) for i, elem in enumerate(component_elems))
            if indices != list(range(len(indices))):
                self.fail(f"{expected_id}: indices da cadeia nao sao sequenciais: {indices}")
            components: list[ET.Element] = []
            for ref in component_refs:
                component = self.resolve(ref, f"{expected_id} componente")
                if component is not None:
                    components.append(component)
                    kinds.append(self.component_kind(component))
                    self.audit_stereo(component, f"{expected_id} componente {self.component_kind(component)}")
            expected_kinds = ["enhance", "volume", "channel_volume", "shelf", "limiter"]
            if kinds != expected_kinds:
                details = [
                    text(component, "./AudioComponent/Component/DisplayName")
                    or text(component, "CustomDisplayName")
                    or text(component, "FilterMatchName")
                    or component.tag
                    for component in components
                ]
                self.fail(
                    f"{expected_id}: cadeia {kinds} / {details}; esperado exatamente {expected_kinds}"
                )
            if len(components) == 5 and kinds == expected_kinds:
                enhance_mix, preview_path = self.audit_enhance(
                    components[0], item, item_props, duration, expected_id
                )
                self.audit_intrinsics(components[1], "volume", expected_id)
                self.audit_intrinsics(components[2], "channel_volume", expected_id)
                self.audit_shelf(components[3], expected_id)
                self.audit_limiter(components[4], expected_id)

        return {
            "id": expected_id,
            "item_object_id": item_oid,
            "name": name,
            "start_ticks": start,
            "end_ticks": end,
            "duration_ticks": duration,
            "frames_48000": frames,
            "media_path": str(media_path) if media_path else None,
            "loudness_analyzed_lufs": loudness,
            "auto_match_gain_linear": gain,
            "auto_match_gain_db": gain_db,
            "auto_matched_lufs": matched_lufs,
            "enhance_mix": enhance_mix,
            "enhance_preview": preview_path,
            "chain": kinds,
        }

    def audit_timeline(self) -> None:
        tracks = self.audio_tracks()
        nonempty: list[tuple[int, ET.Element, list[str]]] = []
        for index, track in enumerate(tracks):
            refs = indexed_refs(
                track.findall("./ClipTrack/ClipItems/TrackItems/TrackItem"),
                "ObjectRef",
            )
            if refs:
                nonempty.append((index, track, refs))
        if len(nonempty) != 1:
            self.fail(f"deve haver uma unica faixa de audio nao vazia; encontradas {len(nonempty)}")
            return
        track_index, _, refs = nonempty[0]
        if len(refs) != len(EXPECTED_IDS):
            self.fail(f"A{track_index + 1} tem {len(refs)} itens; esperado {len(EXPECTED_IDS)}")

        items: list[ET.Element] = []
        for ref in refs:
            item = self.resolve(ref, f"A{track_index + 1} TrackItem")
            if item is None:
                continue
            if item.tag != "AudioClipTrackItem":
                self.fail(f"A{track_index + 1}: TrackItem {ref} e {item.tag}")
                continue
            items.append(item)

        # A ordem autoritativa e temporal, nao a ordem incidental dos ObjectRefs.
        items.sort(key=lambda item: int_text(item, "./ClipTrackItem/TrackItem/Start", 0) or 0)
        for position, expected_id in enumerate(EXPECTED_IDS):
            if position >= len(items):
                break
            row = self.audit_item(items[position], expected_id)
            self.rows.append(row)

        if self.rows:
            if self.rows[0]["start_ticks"] != 0:
                self.fail(f"timeline nao comeca em zero: {self.rows[0]['start_ticks']}")
            for previous, current in zip(self.rows, self.rows[1:]):
                if current["start_ticks"] != previous["end_ticks"]:
                    self.fail(
                        f"gap/overlap entre {previous['id']} e {current['id']}: "
                        f"{previous['end_ticks']} -> {current['start_ticks']}"
                    )
        self.report["timeline"] = {
            "sequence": self.sequence_name,
            "audio_track": track_index + 1,
            "expected_ids": EXPECTED_IDS,
            "excluded_ids": sorted(EXCLUDED_IDS),
            "items": self.rows,
            "total_ticks": self.rows[-1]["end_ticks"] if self.rows else 0,
            "total_frames_48000": sum(row["frames_48000"] for row in self.rows),
        }

    def audit_export_settings(self, concat: Path, final_end: int) -> None:
        assert self.sequence is not None
        seq_props = properties(self.sequence.find("./Node/Properties"))
        raw = seq_props.get("ExportSettings.Info.SaveAsFile")
        if not raw:
            self.fail("sequencia nao preservou ExportSettings.Info.SaveAsFile")
            return
        try:
            info = json.loads(raw)
        except json.JSONDecodeError as exc:
            self.fail(f"ExportSettings.Info.SaveAsFile invalido: {exc}")
            return
        out_path = Path(info.get("OutPath", "")).resolve(strict=False)
        if out_path != concat:
            self.fail(f"ultimo OutPath do projeto={out_path}; concat auditado={concat}")
        if info.get("DoAudio") is not True:
            self.fail("export nao esta marcado para audio")
        if int(info.get("OutPoint", -1)) != final_end or int(info.get("CustomOut", -1)) != final_end:
            self.fail(
                f"faixa de export {info.get('OutPoint')}/{info.get('CustomOut')}; final={final_end}"
            )
        if int(info.get("InPoint", -1)) != 0 or int(info.get("CustomIn", -1)) != 0:
            self.fail(f"export nao inicia em zero: {info.get('InPoint')}/{info.get('CustomIn')}")
        if int(info.get("Range", -1)) != 0:
            self.fail(f"Range do export={info.get('Range')}; esperado Entire Source (0)")

        preset_raw = seq_props.get("ExportSettings.ExportedPreset.SaveAsFile")
        if not preset_raw:
            self.fail("preset de export SaveAsFile ausente do projeto")
            return
        try:
            preset = ET.fromstring(preset_raw)
        except ET.ParseError as exc:
            self.fail(f"preset de export embutido invalido: {exc}")
            return
        if preset.findtext("./AMEPreset/DoAudio") != "true":
            self.fail("preset embutido nao exporta audio")
        if preset.findtext("./AMEPreset/DoVideo") != "false":
            self.fail("preset embutido tambem exporta video")
        if preset.findtext("./AMEPreset/DoEffects") != "false":
            self.fail("preset embutido habilita efeitos/normalizacao de export")
        standard_filters = preset.find("./AMEPreset/StandardFilters")
        # O Premiere sempre serializa este bloco, mesmo com DoEffects=false.
        # Aceite somente o estado neutro observado no WAV-only (sem refs para
        # filtros e sem crop/interpolacao); qualquer outro campo fecha em FAIL.
        neutral_standard_filters = {
            "CropType": "0",
            "UseFrameBlending": "false",
            "CustomStartTime": "-101606400000000000",
            "TimeInterpolationType": "0",
        }
        if standard_filters is not None:
            actual_standard_filters = {
                child.tag: (child.text or "").strip()
                for child in list(standard_filters)
            }
            if actual_standard_filters != neutral_standard_filters:
                self.fail(
                    "StandardFilters do export nao esta no estado neutro: "
                    f"{actual_standard_filters}"
                )
        params = {
            elem.findtext("ParamIdentifier"): elem.findtext("ParamValue")
            for elem in preset.findall("ExporterParam")
            if elem.findtext("ParamIdentifier")
        }
        expected = {
            "ADBEAudioRatePerSecond": "48000",
            "ADBEAudioNumChannels": "2",
            "ADBEAudioSampleType": "2",
        }
        for key, value in expected.items():
            if params.get(key) != value:
                self.fail(f"preset de export {key}={params.get(key)!r}; esperado {value!r}")

    def parse_wav(self, path: Path) -> dict[str, Any] | None:
        try:
            file_size = path.stat().st_size
            with path.open("rb") as fh:
                header = fh.read(12)
                if len(header) != 12 or header[0:4] not in {b"RIFF", b"RF64"} or header[8:12] != b"WAVE":
                    raise ValueError("cabecalho RIFF/RF64 WAVE invalido")
                riff_kind = header[0:4].decode("ascii")
                fmt_data = None
                data_size = None
                ds64_data_size = None
                while True:
                    chunk_header = fh.read(8)
                    if not chunk_header:
                        break
                    if len(chunk_header) != 8:
                        raise ValueError("chunk truncado")
                    chunk_id, chunk_size = struct.unpack("<4sI", chunk_header)
                    effective_chunk_size = chunk_size
                    if chunk_id == b"fmt ":
                        payload = fh.read(chunk_size)
                        if len(payload) != chunk_size:
                            raise ValueError("chunk fmt truncado")
                        fmt_data = payload
                    elif chunk_id == b"data":
                        data_size = chunk_size
                        skip = (
                            ds64_data_size
                            if chunk_size == 0xFFFFFFFF and ds64_data_size is not None
                            else chunk_size
                        )
                        effective_chunk_size = skip
                        if fh.tell() + skip > file_size:
                            raise ValueError("chunk data truncado")
                        fh.seek(skip, os.SEEK_CUR)
                    elif chunk_id == b"ds64":
                        payload = fh.read(chunk_size)
                        if len(payload) != chunk_size or len(payload) < 16:
                            raise ValueError("chunk ds64 truncado")
                        _, ds64_data_size = struct.unpack_from("<QQ", payload, 0)
                    else:
                        if fh.tell() + chunk_size > file_size:
                            raise ValueError(f"chunk {chunk_id!r} truncado")
                        fh.seek(chunk_size, os.SEEK_CUR)
                    if effective_chunk_size & 1 and fh.tell() < file_size:
                        fh.seek(1, os.SEEK_CUR)
            if fmt_data is None or len(fmt_data) < 16 or data_size is None:
                raise ValueError("chunks fmt/data ausentes")
            audio_format, channels, rate, byte_rate, block_align, bits = struct.unpack_from(
                "<HHIIHH", fmt_data, 0
            )
            subtype = audio_format
            valid_bits = bits
            if audio_format == 0xFFFE:
                if len(fmt_data) < 40:
                    raise ValueError("WAVE_FORMAT_EXTENSIBLE truncado")
                valid_bits = struct.unpack_from("<H", fmt_data, 18)[0]
                subtype = struct.unpack_from("<I", fmt_data, 24)[0]
            effective_data_size = ds64_data_size if data_size == 0xFFFFFFFF else data_size
            if effective_data_size is None or effective_data_size % block_align:
                raise ValueError("tamanho de data nao e multiplo de block_align")
            frames = effective_data_size // block_align
            return {
                "path": str(path),
                "size": path.stat().st_size,
                "sha256": sha256_path(path),
                "riff_kind": riff_kind,
                "audio_format": audio_format,
                "subtype": subtype,
                "channels": channels,
                "sample_rate": rate,
                "byte_rate": byte_rate,
                "block_align": block_align,
                "bits_per_sample": bits,
                "valid_bits": valid_bits,
                "data_bytes": effective_data_size,
                "frames": frames,
            }
        except Exception as exc:  # noqa: BLE001
            self.fail(f"WAV concat invalido: {exc}")
            return None

    def audit_concat(self) -> None:
        if self.concat is None:
            return
        if self.sequence is None or not self.rows:
            self.fail("concat nao pode ser auditado sem uma timeline valida carregada")
            return
        if not under(self.concat, WORKSPACE_ROOT):
            self.fail(f"concat fora do workspace seguro: {self.concat}")
            return
        if not self.concat.is_file():
            self.fail(f"concat inexistente: {self.concat}")
            return
        wav = self.parse_wav(self.concat)
        if wav is None:
            return
        expected_format = {
            "subtype": 1,
            "channels": 2,
            "sample_rate": SAMPLE_RATE,
            "block_align": 6,
            "bits_per_sample": 24,
            "valid_bits": 24,
        }
        for key, expected in expected_format.items():
            if wav.get(key) != expected:
                self.fail(f"concat {key}={wav.get(key)}; esperado {expected}")
        expected_frames = sum(row["frames_48000"] for row in self.rows)
        if wav["frames"] != expected_frames:
            self.fail(f"concat tem {wav['frames']} frames; timeline soma {expected_frames}")
        final_end = self.rows[-1]["end_ticks"] if self.rows else 0
        self.audit_export_settings(self.concat, final_end)
        boundaries = []
        cursor = 0
        for row in self.rows:
            boundaries.append(
                {
                    "id": row["id"],
                    "start_frame": cursor,
                    "end_frame": cursor + row["frames_48000"],
                }
            )
            cursor += row["frames_48000"]
        wav["boundaries"] = boundaries
        self.report["concat"] = wav

    def run(self) -> dict[str, Any]:
        self.load_manifest()
        before = self.load_project()
        if self.root is not None:
            self.audit_all_project_media()
        if self.sequence is not None:
            self.audit_timeline()
        self.audit_concat()
        if before is not None and self.project.is_file():
            after = snapshot(self.project)
            if after != before:
                self.fail("o .prproj mudou durante a auditoria; resultado invalido")
            self.report.setdefault("project", {})["unchanged_during_audit"] = after == before
        self.report["status"] = "PASS" if not self.errors else "FAIL"
        self.report["errors"] = self.errors
        self.report["warnings"] = self.warnings
        self.report["contract"] = {
            "expected_ids": EXPECTED_IDS,
            "excluded_ids": sorted(EXCLUDED_IDS),
            "expected_chain": ["enhance", "volume", "channel_volume", "shelf", "limiter"],
            "enhance_mix": 1.0,
            "enhance_e100_opaque_sha256": ENHANCE_E100_OPAQUE_SHA256,
            "auto_match_target_lufs": -23.0,
            "shelf_opaque_sha256": SHELF_OPAQUE_SHA256,
            "limiter_opaque_sha256": LIMITER_OPAQUE_SHA256,
            "sample_rate": SAMPLE_RATE,
            "channels": 2,
        }
        return self.report


def markdown_report(report: dict[str, Any]) -> str:
    status = report.get("status", "FAIL")
    lines = [
        "# Auditoria — fluxo aprovado Premiere (20 clipes)",
        "",
        f"Resultado: **{status}**",
        "",
    ]
    project = report.get("project", {})
    if project:
        lines.extend(
            [
                f"- Projeto: `{project.get('path')}`",
                f"- SHA-256: `{project.get('sha256')}`",
                f"- Imutavel durante a leitura: `{project.get('unchanged_during_audit')}`",
                "",
            ]
        )
    errors = report.get("errors", [])
    if errors:
        lines.extend(["## Falhas", ""])
        lines.extend(f"- {message}" for message in errors)
        lines.append("")
    warnings = report.get("warnings", [])
    if warnings:
        lines.extend(["## Avisos", ""])
        lines.extend(f"- {message}" for message in warnings)
        lines.append("")
    rows = report.get("timeline", {}).get("items", [])
    if rows:
        lines.extend(
            [
                "## Itens da timeline",
                "",
                "| ID | frames | E100 | analisado LUFS | ganho AM dB | resultado LUFS | cadeia |",
                "|---:|---:|---:|---:|---:|---:|---|",
            ]
        )
        for row in rows:
            loud = row.get("loudness_analyzed_lufs")
            gain = row.get("auto_match_gain_db")
            matched = row.get("auto_matched_lufs")
            lines.append(
                f"| {row['id']} | {row['frames_48000']} | "
                f"{row.get('enhance_mix') if row.get('enhance_mix') is not None else '—'} | "
                f"{loud:.4f} | {gain:.4f} | {matched:.4f} | "
                f"{' → '.join(row.get('chain', []))} |"
                if loud is not None and gain is not None and matched is not None
                else f"| {row['id']} | {row['frames_48000']} | — | — | — | — | "
                f"{' → '.join(row.get('chain', []))} |"
            )
        lines.append("")
    concat = report.get("concat")
    if concat:
        lines.extend(
            [
                "## Concat real",
                "",
                f"- WAV: `{concat.get('path')}`",
                f"- SHA-256: `{concat.get('sha256')}`",
                f"- Formato: {concat.get('sample_rate')} Hz, {concat.get('channels')} canais, "
                f"PCM {concat.get('valid_bits')} bit",
                f"- Frames: {concat.get('frames')}",
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def safe_write(path: Path, payload: str) -> None:
    resolved = path.resolve(strict=False)
    if not under(resolved, WORKSPACE_ROOT):
        raise ValueError(f"saida fora do workspace: {resolved}")
    resolved.parent.mkdir(parents=True, exist_ok=True)
    resolved.write_text(payload, encoding="utf-8")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", type=Path, required=True)
    parser.add_argument("--sequence", required=True, help="nome exato da sequencia de 20 clipes")
    parser.add_argument("--allowed-media-root", type=Path, required=True)
    parser.add_argument("--manifest", type=Path)
    parser.add_argument("--concat", type=Path)
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--report-md", type=Path)
    parser.add_argument(
        "--allow-missing-preview-files",
        action="store_true",
        help="modo diagnostico; o modo probatorio padrao exige os CFA de Enhance",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    auditor = Auditor(
        project=args.project,
        sequence_name=args.sequence,
        allowed_media_root=args.allowed_media_root,
        manifest=args.manifest,
        concat=args.concat,
        require_preview_files=not args.allow_missing_preview_files,
    )
    report = auditor.run()
    rendered = markdown_report(report)
    if args.report_json:
        safe_write(args.report_json, json.dumps(report, indent=2, ensure_ascii=False) + "\n")
    if args.report_md:
        safe_write(args.report_md, rendered)
    print(rendered, end="")
    return 0 if report["status"] == "PASS" else 2


if __name__ == "__main__":
    sys.exit(main())

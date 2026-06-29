"""
proliant.oneview.enclosures
~~~~~~~~~~~~~~~~~~~~~~~~~~~
Enclosures, Enclosure Groups (EG), and Logical Enclosures (LE).

Key endpoints:
  GET /rest/enclosures              -> physical enclosures
  GET /rest/enclosure-groups        -> enclosure groups
  GET /rest/logical-enclosures      -> logical enclosures
"""
from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from proliant.oneview.client import OneViewClient


# ── Enclosures ────────────────────────────────────────────────────────────────

def parse_enclosure(raw: dict) -> dict:
    return {
        "name":   raw.get("name", ""),
        "model":  raw.get("enclosureModel", ""),
        "serial": raw.get("serialNumber", ""),
        "state":  raw.get("state", ""),
        "status": raw.get("status", ""),
        "uri":    raw.get("uri", ""),
    }


async def list_enclosures(client: "OneViewClient") -> list[dict]:
    raw = await client.get_all("/rest/enclosures")
    return sorted([parse_enclosure(e) for e in raw], key=lambda e: e["name"])


# ── Enclosure Groups (EG) ─────────────────────────────────────────────────────

def parse_eg(raw: dict, lig_map: dict[str, str]) -> dict:
    lig_names = sorted({
        lig_map.get(m.get("logicalInterconnectGroupUri", ""), "")
        for m in raw.get("interconnectBayMappings", [])
        if m.get("logicalInterconnectGroupUri")
    })
    return {
        "name":      raw.get("name", ""),
        "lig_names": lig_names,
        "status":    raw.get("status", ""),
        "uri":       raw.get("uri", ""),
    }


async def list_enclosure_groups(client: "OneViewClient") -> list[dict]:
    raw_egs, raw_ligs = await asyncio.gather(
        client.get_all("/rest/enclosure-groups"),
        client.get_all("/rest/logical-interconnect-groups"),
    )
    lig_map = {lg["uri"]: lg.get("name", "") for lg in raw_ligs}
    return sorted([parse_eg(eg, lig_map) for eg in raw_egs], key=lambda eg: eg["name"])


# ── Logical Enclosures (LE) ───────────────────────────────────────────────────

def parse_le(raw: dict, eg_map: dict, enc_map: dict, li_map: dict) -> dict:
    enc_names = sorted([enc_map.get(u, u.rsplit("/", 1)[-1]) for u in raw.get("enclosureUris", [])])
    li_names  = sorted([li_map.get(u,  u.rsplit("/", 1)[-1]) for u in raw.get("logicalInterconnectUris", [])])
    return {
        "name":       raw.get("name", ""),
        "eg_name":    eg_map.get(raw.get("enclosureGroupUri", ""), ""),
        "enclosures": enc_names,
        "lis":        li_names,
        "state":      raw.get("state", ""),
        "status":     raw.get("status", ""),
        "uri":        raw.get("uri", ""),
    }


async def list_logical_enclosures(client: "OneViewClient") -> list[dict]:
    raw_les, raw_egs, raw_encs, raw_lis = await asyncio.gather(
        client.get_all("/rest/logical-enclosures"),
        client.get_all("/rest/enclosure-groups"),
        client.get_all("/rest/enclosures"),
        client.get_all("/rest/logical-interconnects"),
    )
    eg_map  = {eg["uri"]: eg.get("name", "") for eg in raw_egs}
    enc_map = {e["uri"]:  e.get("name",  "") for e in raw_encs}
    li_map  = {li["uri"]: li.get("name", "") for li in raw_lis}
    les = [parse_le(le, eg_map, enc_map, li_map) for le in raw_les]
    return sorted(les, key=lambda le: le["name"])

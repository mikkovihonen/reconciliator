"""OAI-PMH 2.0 Authority Provider endpoint for harvesting Virtual Authority File records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from lxml import etree

from reconciliator.authority.serializers.marc21 import MARC21AuthoritySerializer
from reconciliator.config import AppConfig, OAIPMHServerConfig
from reconciliator.database import WEMIRepository

router = APIRouter(prefix="/oai", tags=["OAI-PMH Authority Provider"])

OAI_NS = "http://www.openarchives.org/OAI/2.0/"
OAI_DC_NS = "http://www.openarchives.org/OAI/2.0/oai_dc/"
DC_NS = "http://purl.org/dc/elements/1.1/"
MARC_NS = "http://www.loc.gov/MARC21/slim"


def _get_oai_config(request: Request) -> OAIPMHServerConfig:
    app_cfg: AppConfig = getattr(request.app.state, "config", None) or AppConfig()
    return app_cfg.virtual_authority.oai_pmh_server


def _create_oai_root(verb: str, request_url: str) -> tuple[etree._Element, etree._Element]:
    root = etree.Element(f"{{{OAI_NS}}}OAI-PMH", nsmap={None: OAI_NS})
    response_date = etree.SubElement(root, f"{{{OAI_NS}}}responseDate")
    response_date.text = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    req_elem = etree.SubElement(root, f"{{{OAI_NS}}}request")
    req_elem.set("verb", verb)
    req_elem.text = request_url

    verb_container = etree.SubElement(root, f"{{{OAI_NS}}}{verb}")
    return root, verb_container


@router.get("/authority")
def handle_oai_request(
    request: Request,
    verb: str = Query(..., description="OAI-PMH verb"),
    identifier: Optional[str] = Query(None),
    metadataPrefix: Optional[str] = Query(None),
    set_spec: Optional[str] = Query(None, alias="set"),
    from_date: Optional[str] = Query(None, alias="from"),
    until_date: Optional[str] = Query(None, alias="until"),
    resumptionToken: Optional[str] = Query(None),
) -> Response:
    """Handle OAI-PMH 2.0 requests for authority records."""
    repo: WEMIRepository = getattr(request.app.state, "repo", None)
    cfg = _get_oai_config(request)
    req_url = str(request.url).split("?")[0]

    # --- Verb: Identify ---
    if verb == "Identify":
        root, container = _create_oai_root("Identify", req_url)
        name = etree.SubElement(container, f"{{{OAI_NS}}}repositoryName")
        name.text = cfg.repository_name
        base_url = etree.SubElement(container, f"{{{OAI_NS}}}baseURL")
        base_url.text = req_url
        proto = etree.SubElement(container, f"{{{OAI_NS}}}protocolVersion")
        proto.text = "2.0"
        admin = etree.SubElement(container, f"{{{OAI_NS}}}adminEmail")
        admin.text = cfg.admin_email
        earliest = etree.SubElement(container, f"{{{OAI_NS}}}earliestDatestamp")
        earliest.text = cfg.earliest_datestamp
        deleted = etree.SubElement(container, f"{{{OAI_NS}}}deletedRecord")
        deleted.text = "transient"
        granularity = etree.SubElement(container, f"{{{OAI_NS}}}granularity")
        granularity.text = "YYYY-MM-DDThh:mm:ssZ"
        xml_str = etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="utf-8")
        return Response(content=xml_str, media_type="text/xml")

    # --- Verb: ListMetadataFormats ---
    if verb == "ListMetadataFormats":
        root, container = _create_oai_root("ListMetadataFormats", req_url)
        # 1. marc21_authority
        mf1 = etree.SubElement(container, f"{{{OAI_NS}}}metadataFormat")
        etree.SubElement(mf1, f"{{{OAI_NS}}}metadataPrefix").text = "marc21_authority"
        etree.SubElement(
            mf1, f"{{{OAI_NS}}}schema"
        ).text = "http://www.loc.gov/standards/marcxml/schema/MARC21slim.xsd"
        etree.SubElement(mf1, f"{{{OAI_NS}}}metadataNamespace").text = MARC_NS

        # 2. oai_dc
        mf2 = etree.SubElement(container, f"{{{OAI_NS}}}metadataFormat")
        etree.SubElement(mf2, f"{{{OAI_NS}}}metadataPrefix").text = "oai_dc"
        etree.SubElement(
            mf2, f"{{{OAI_NS}}}schema"
        ).text = "http://www.openarchives.org/OAI/2.0/oai_dc.xsd"
        etree.SubElement(mf2, f"{{{OAI_NS}}}metadataNamespace").text = OAI_DC_NS

        xml_str = etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="utf-8")
        return Response(content=xml_str, media_type="text/xml")

    # --- Verb: ListSets ---
    if verb == "ListSets":
        root, container = _create_oai_root("ListSets", req_url)
        s1 = etree.SubElement(container, f"{{{OAI_NS}}}set")
        etree.SubElement(s1, f"{{{OAI_NS}}}setSpec").text = "agents"
        etree.SubElement(s1, f"{{{OAI_NS}}}setName").text = "Agent Authority Clusters"

        s2 = etree.SubElement(container, f"{{{OAI_NS}}}set")
        etree.SubElement(s2, f"{{{OAI_NS}}}setSpec").text = "works"
        etree.SubElement(s2, f"{{{OAI_NS}}}setName").text = "Work / Uniform Title Clusters"

        xml_str = etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="utf-8")
        return Response(content=xml_str, media_type="text/xml")

    # --- Verb: GetRecord ---
    if verb == "GetRecord":
        if not identifier or not metadataPrefix:
            raise HTTPException(
                status_code=400, detail="Missing required 'identifier' or 'metadataPrefix'"
            )
        if not repo:
            raise HTTPException(status_code=404, detail="Repository not initialized")

        root, container = _create_oai_root("GetRecord", req_url)
        clean_id = identifier.replace("oai:reconciliator:", "")
        cluster = repo.get_agent_cluster_full(clean_id)
        if not cluster:
            raise HTTPException(status_code=404, detail=f"Authority record {identifier} not found")

        rec_elem = etree.SubElement(container, f"{{{OAI_NS}}}record")
        header = etree.SubElement(rec_elem, f"{{{OAI_NS}}}header")
        etree.SubElement(header, f"{{{OAI_NS}}}identifier").text = identifier
        etree.SubElement(header, f"{{{OAI_NS}}}datestamp").text = datetime.now(
            timezone.utc
        ).strftime("%Y-%m-%dT%H:%M:%SZ")
        etree.SubElement(header, f"{{{OAI_NS}}}setSpec").text = "agents"

        meta_container = etree.SubElement(rec_elem, f"{{{OAI_NS}}}metadata")
        if metadataPrefix == "marc21_authority":
            serializer = MARC21AuthoritySerializer()
            m_xml = serializer.serialize_agent_cluster(cluster)
            m_tree = etree.fromstring(m_xml.encode("utf-8"))
            meta_container.append(m_tree)
        else:
            dc_elem = etree.SubElement(
                meta_container, f"{{{OAI_DC_NS}}}dc", nsmap={"oai_dc": OAI_DC_NS, "dc": DC_NS}
            )
            etree.SubElement(dc_elem, f"{{{DC_NS}}}title").text = cluster.canonical_name
            etree.SubElement(dc_elem, f"{{{DC_NS}}}identifier").text = cluster.cluster_id

        xml_str = etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="utf-8")
        return Response(content=xml_str, media_type="text/xml")

    # --- Verb: ListRecords / ListIdentifiers ---
    if verb in ("ListRecords", "ListIdentifiers"):
        if not metadataPrefix and not resumptionToken:
            raise HTTPException(status_code=400, detail="Missing required 'metadataPrefix'")
        if not repo:
            return Response(content="<error>No repository</error>", media_type="text/xml")

        root, container = _create_oai_root(verb, req_url)
        limit = cfg.records_per_page
        offset = 0
        if resumptionToken and resumptionToken.isdigit():
            offset = int(resumptionToken)

        cluster_type = "agent" if (set_spec or "agents") == "agents" else "work"
        items, total = repo.list_authority_clusters_modified_since(
            cluster_type=cluster_type, limit=limit, offset=offset
        )

        serializer = MARC21AuthoritySerializer()
        for it in items:
            cid = it.get("cluster_id")
            c_name = it.get("canonical_name") or it.get("canonical_title") or "Unknown"

            if verb == "ListIdentifiers":
                header = etree.SubElement(container, f"{{{OAI_NS}}}header")
                etree.SubElement(
                    header, f"{{{OAI_NS}}}identifier"
                ).text = f"oai:reconciliator:{cid}"
                etree.SubElement(header, f"{{{OAI_NS}}}datestamp").text = datetime.now(
                    timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                etree.SubElement(header, f"{{{OAI_NS}}}setSpec").text = "agents"
            else:
                rec_elem = etree.SubElement(container, f"{{{OAI_NS}}}record")
                header = etree.SubElement(rec_elem, f"{{{OAI_NS}}}header")
                etree.SubElement(
                    header, f"{{{OAI_NS}}}identifier"
                ).text = f"oai:reconciliator:{cid}"
                etree.SubElement(header, f"{{{OAI_NS}}}datestamp").text = datetime.now(
                    timezone.utc
                ).strftime("%Y-%m-%dT%H:%M:%SZ")
                etree.SubElement(header, f"{{{OAI_NS}}}setSpec").text = "agents"

                meta_container = etree.SubElement(rec_elem, f"{{{OAI_NS}}}metadata")
                cluster = repo.get_agent_cluster_full(cid)
                if cluster and metadataPrefix == "marc21_authority":
                    m_xml = serializer.serialize_agent_cluster(cluster)
                    m_tree = etree.fromstring(m_xml.encode("utf-8"))
                    meta_container.append(m_tree)
                else:
                    dc_elem = etree.SubElement(
                        meta_container,
                        f"{{{OAI_DC_NS}}}dc",
                        nsmap={"oai_dc": OAI_DC_NS, "dc": DC_NS},
                    )
                    etree.SubElement(dc_elem, f"{{{DC_NS}}}title").text = c_name
                    etree.SubElement(dc_elem, f"{{{DC_NS}}}identifier").text = str(cid)

        # Resumption token if more records remain
        if offset + limit < total:
            next_token = etree.SubElement(container, f"{{{OAI_NS}}}resumptionToken")
            next_token.set("completeListSize", str(total))
            next_token.set("cursor", str(offset))
            next_token.text = str(offset + limit)

        xml_str = etree.tostring(root, pretty_print=True, xml_declaration=True, encoding="utf-8")
        return Response(content=xml_str, media_type="text/xml")

    raise HTTPException(status_code=400, detail=f"Unsupported OAI-PMH verb: {verb}")

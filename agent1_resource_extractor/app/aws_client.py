"""
AWS Client: estrae risorse via AWS Config (select_resource_config) con fallback
a describe_* / list_buckets quando Config non è disponibile (es. moto in CI).
AssumeRole opzionale.

Multi-regione: il parametro ``region`` accetta una singola regione
("eu-south-1"), una lista separata da virgole ("eu-south-1,eu-west-1") oppure
"all" (default) = tutte le regioni abilitate sull'account (ec2.describe_regions).
Con piu' regioni l'estrazione gira in parallelo (un client/sessione per
regione) e le risorse globali (IAM, S3) vengono deduplicate per resource_id.
"""
from __future__ import annotations
import json
import logging
import os
import re
from concurrent.futures import ThreadPoolExecutor
from typing import Callable, Optional

import boto3
from pydantic import BaseModel

from .tagging_strategy import analyze_tagging

logger = logging.getLogger(__name__)

DEFAULT_RESOURCE_TYPES: list[str] = [
    "AWS::EC2::Instance",
    "AWS::EC2::Volume",
    "AWS::EC2::VPC",
    "AWS::EC2::Subnet",
    "AWS::EC2::SecurityGroup",
    "AWS::S3::Bucket",
]

# Tipi "di servizio" di AWS Config stesso: non sono risorse infrastrutturali e
# vanno esclusi dalla scoperta dinamica di list_all_resources_from_config().
_CONFIG_META_TYPES: set[str] = {
    "AWS::Config::ResourceCompliance",
    "AWS::Config::ConfigurationRecorder",
}

ALL_REGIONS = "all"
DEFAULT_HOME_REGION = "eu-south-1"
_MAX_REGION_WORKERS = 8

# Nota: il campo regione di AWS Config e' "awsRegion" (non "region").
_CONFIG_SELECT_FIELDS = (
    "resourceId, resourceName, arn, resourceType, accountId, awsRegion, "
    "availabilityZone, resourceCreationTime, configuration, "
    "supplementaryConfiguration, tags"
)
# Set minimo usato come retry se l'espressione completa viene rifiutata.
_CONFIG_SELECT_FIELDS_MINIMAL = "resourceId, arn, resourceType, accountId, awsRegion, configuration, tags"


def parse_region_spec(region: str | None) -> list[str] | None:
    """
    "eu-south-1" -> ["eu-south-1"]; "a,b" -> ["a", "b"];
    None / "" / "all" -> None (= tutte le regioni abilitate, da scoprire).
    """
    spec = (region or "").strip()
    if not spec or spec.lower() == ALL_REGIONS:
        return None
    return list(dict.fromkeys(r.strip() for r in spec.split(",") if r.strip())) or None


def _default_home_region() -> str:
    return (
        os.environ.get("AWS_REGION")
        or os.environ.get("AWS_DEFAULT_REGION")
        or DEFAULT_HOME_REGION
    )


def _jsonable(obj):
    """Rende serializzabile in JSON un oggetto boto3 (datetime -> str)."""
    return json.loads(json.dumps(obj, default=str))

# Normalizza il relationshipName in linguaggio naturale restituito da AWS Config
# nel vocabolario chiuso gia' usato dalla pipeline (vedi README, Agente 4).
_RELATIONSHIP_TYPE_PATTERNS: list[tuple[re.Pattern, str]] = [
    (re.compile(r"attached", re.I), "ATTACHED_TO"),
    (re.compile(r"security group", re.I), "SECURED_BY"),
    (re.compile(r"contains|is contained in|is the subnet for", re.I), "CONTAINS"),
]


def _normalize_relationship_type(relationship_name: str) -> str:
    for pattern, rel_type in _RELATIONSHIP_TYPE_PATTERNS:
        if pattern.search(relationship_name or ""):
            return rel_type
    return "DEPENDS_ON"

_TAGGING_FILTER: dict[str, str] = {
    "AWS::EC2::Instance":      "ec2:instance",
    "AWS::EC2::Volume":        "ec2:volume",
    "AWS::EC2::VPC":           "ec2:vpc",
    "AWS::EC2::Subnet":        "ec2:subnet",
    "AWS::EC2::SecurityGroup": "ec2:security-group",
    "AWS::S3::Bucket":         "s3",
}


class Relationship(BaseModel):
    type: str
    target_resource_id: str


class NormalizedResource(BaseModel):
    resource_id: str
    account_id: str
    region: str
    resource_type: str
    current_tags: dict[str, str] = {}
    attributes: dict = {}
    relationships: list[Relationship] = []


class AccountMismatchError(Exception):
    """Le credenziali AWS appartengono a un account diverso da quello del job."""

    def __init__(self, expected: str, actual: str) -> None:
        self.expected = expected
        self.actual = actual
        super().__init__(
            f"Le credenziali AWS attive sono dell'account {actual}, "
            f"ma il job è sull'account {expected}: cambia profilo/credenziali e riprova"
        )


class AWSClient:
    def __init__(
        self,
        account_id: str,
        region: str | None = ALL_REGIONS,
        assume_role_arn: Optional[str] = None,
        *,
        _credentials: Optional[dict] = None,
    ) -> None:
        self.account_id = account_id
        requested = parse_region_spec(region)
        self.region = requested[0] if requested else _default_home_region()
        # AssumeRole una sola volta: i client per-regione riusano le credenziali.
        self._credentials = (
            _credentials if _credentials is not None else self._assume_role(assume_role_arn)
        )
        self._session = self._new_session(self.region)
        self.regions: list[str] = requested or self._discover_enabled_regions()
        if len(self.regions) == 1 and self.regions[0] != self.region:
            self.region = self.regions[0]
            self._session = self._new_session(self.region)

    def verify_account(self) -> None:
        """
        Verifica che le credenziali attive appartengano all'account richiesto.
        Senza questo controllo un'estrazione lanciata con il profilo sbagliato
        scrive le risorse di un altro account sul job.
        """
        actual = self._session.client("sts").get_caller_identity()["Account"]
        if actual != self.account_id:
            raise AccountMismatchError(self.account_id, actual)

    @property
    def is_multi_region(self) -> bool:
        return len(self.regions) > 1

    @staticmethod
    def _assume_role(assume_role_arn: Optional[str]) -> dict:
        if not assume_role_arn:
            return {}
        sts = boto3.client("sts")
        creds = sts.assume_role(
            RoleArn=assume_role_arn,
            RoleSessionName="finops-extractor",
            DurationSeconds=3600,
        )["Credentials"]
        return {
            "aws_access_key_id": creds["AccessKeyId"],
            "aws_secret_access_key": creds["SecretAccessKey"],
            "aws_session_token": creds["SessionToken"],
        }

    def _new_session(self, region: str) -> boto3.Session:
        return boto3.Session(region_name=region, **self._credentials)

    def _discover_enabled_regions(self) -> list[str]:
        """Regioni abilitate sull'account (opt-in incluse solo se attivate)."""
        try:
            resp = self._session.client("ec2", region_name=self.region).describe_regions()
            regions = sorted(r["RegionName"] for r in resp.get("Regions", []))
        except Exception as exc:
            logger.warning("describe_regions fallita (%s): uso solo %s", exc, self.region)
            return [self.region]
        return regions or [self.region]

    def for_region(self, region: str) -> "AWSClient":
        """Client single-region che riusa account e credenziali di questo."""
        return AWSClient(self.account_id, region, _credentials=self._credentials)

    def _per_region(
        self, call: Callable[["AWSClient"], list["NormalizedResource"]]
    ) -> list["NormalizedResource"]:
        """
        Esegue ``call`` su un client per ciascuna regione, in parallelo
        (una boto3.Session per thread: le Session non sono thread-safe).
        Un errore in una regione (es. regione non autorizzata) viene loggato
        e non blocca le altre. Il risultato e' ordinato come self.regions e
        deduplicato per resource_id (risorse globali: IAM, S3).
        """
        clients = [self.for_region(r) for r in self.regions]
        by_region: dict[str, list[NormalizedResource]] = {}

        def _run(client: "AWSClient") -> None:
            try:
                by_region[client.region] = call(client)
            except Exception as exc:
                logger.warning("Estrazione fallita in %s: %s", client.region, exc)
                by_region[client.region] = []

        with ThreadPoolExecutor(max_workers=min(_MAX_REGION_WORKERS, len(clients))) as pool:
            list(pool.map(_run, clients))

        seen: set[str] = set()
        results: list[NormalizedResource] = []
        for region in self.regions:
            for r in by_region.get(region, []):
                if r.resource_id in seen:
                    continue
                seen.add(r.resource_id)
                results.append(r)
        logger.info(
            "Estrazione multi-regione: %d risorse da %d regioni (%s)",
            len(results), len(self.regions),
            ", ".join(f"{k}={len(v)}" for k, v in by_region.items() if v),
        )
        return results

    def list_resources(
        self, resource_types: list[str] | None = None
    ) -> list[NormalizedResource]:
        if self.is_multi_region:
            return self._per_region(lambda c: c.list_resources(resource_types))

        types = resource_types or DEFAULT_RESOURCE_TYPES

        # Augment with live tags from Resource Groups Tagging API
        tags_by_arn = self._get_tags_by_arn(types)

        results: list[NormalizedResource] = []
        for rt in types:
            logger.info("Extracting %s", rt)
            # Try Config first; fall back to describe_* if unavailable
            items = self._config_query(rt)
            if items is None:
                logger.info("  Config non disponibile per %s, uso describe_*", rt)
                resources = self._describe_fallback(rt, tags_by_arn)
            else:
                resources = [
                    r for item in items
                    for r in [self._normalize(item, tags_by_arn)]
                    if r is not None
                ]
            logger.info("  %d risorse trovate per %s", len(resources), rt)
            results.extend(resources)

        return results

    def discover_config_resource_types(self) -> list[str]:
        """
        Interroga AWS Config per scoprire dinamicamente TUTTI i resourceType
        effettivamente presenti nell'account/regione (via GROUP BY), invece di
        limitarsi ai 6 tipi hardcoded in DEFAULT_RESOURCE_TYPES. Richiede che
        il Configuration Recorder sia attivo con allSupported=true (o almeno
        i tipi che si vogliono scoprire). In multi-regione: unione dei tipi.
        """
        if self.is_multi_region:
            found: set[str] = set()
            for client in (self.for_region(r) for r in self.regions):
                found.update(client.discover_config_resource_types())
            return sorted(found)

        cfg = self._session.client("config", region_name=self.region)
        types: set[str] = set()
        kwargs: dict = {"Expression": "SELECT resourceType GROUP BY resourceType", "Limit": 100}
        try:
            while True:
                resp = cfg.select_resource_config(**kwargs)
                for s in resp.get("Results", []):
                    rt = json.loads(s).get("resourceType")
                    if rt:
                        types.add(rt)
                next_token = resp.get("NextToken")
                if not next_token:
                    break
                kwargs["NextToken"] = next_token
        except Exception as exc:
            logger.warning("Impossibile enumerare i resourceType da Config: %s", exc)
            return []
        return sorted(types - _CONFIG_META_TYPES)

    def list_all_resources_from_config(self) -> list[NormalizedResource]:
        """
        Estrae TUTTE le risorse instanziate nell'account, partendo dal servizio
        AWS Config (nessuna lista di resource type hardcoded): scopre i tipi
        presenti via discover_config_resource_types(), poi per ciascuno
        interroga select_resource_config includendo anche le relationships
        native di Config (risolte in ARN quando possibile e normalizzate nel
        vocabolario CONTAINS/SECURED_BY/ATTACHED_TO/DEPENDS_ON).

        Per ciascuna risorsa arricchisce inoltre attributes["tagging_analysis"]
        con il confronto tra i tag correnti e la CINECA Tagging Strategy v1.5
        (tag cineca:* mandatory/operativi mancanti + suggerimenti best-effort).

        In multi-regione ogni regione e' estratta in parallelo (le relationships
        sono risolte all'interno della regione).
        """
        if self.is_multi_region:
            return self._per_region(lambda c: c.list_all_resources_from_config())

        types = self.discover_config_resource_types()
        if not types:
            return []

        tags_by_arn = self._get_tags_by_arn(types)

        raw_items: list[dict] = []
        for rt in types:
            logger.info("Extracting %s (config-inventory)", rt)
            items = self._config_query(rt, select_fields=_CONFIG_SELECT_FIELDS + ", relationships")
            if items:
                raw_items.extend(items)

        # Config non include l'ARN della risorsa "correlata" in relationships,
        # solo resourceType/resourceId: lo risolviamo con un indice costruito
        # su tutte le risorse appena estratte.
        arn_by_resource_id: dict[str, str] = {
            item["resourceId"]: item["arn"]
            for item in raw_items
            if item.get("resourceId") and item.get("arn")
        }

        results: list[NormalizedResource] = []
        for item in raw_items:
            resource = self._normalize(item, tags_by_arn)
            if resource is None:
                continue

            resource.relationships = [
                Relationship(
                    type=_normalize_relationship_type(rel.get("relationshipName", "")),
                    target_resource_id=arn_by_resource_id.get(
                        rel.get("resourceId", ""),
                        f"{rel.get('resourceType', 'unknown')}:{rel.get('resourceId', 'unknown')}",
                    ),
                )
                for rel in (item.get("relationships") or [])
                if rel.get("resourceId")
            ]

            name = resource.current_tags.get("Name")
            resource.attributes["tagging_analysis"] = analyze_tagging(
                resource.resource_type, resource.current_tags, name
            )
            results.append(resource)

        return results

    def _config_query(
        self, resource_type: str, select_fields: str = _CONFIG_SELECT_FIELDS
    ) -> list[dict] | None:
        """
        Interroga AWS Config. Ritorna None se Config non è disponibile
        (NotImplementedError da moto, o errore di servizio).
        """
        cfg = self._session.client("config", region_name=self.region)
        expr = f"SELECT {select_fields} WHERE resourceType = '{resource_type}'"
        items: list[dict] = []
        kwargs: dict = {"Expression": expr, "Limit": 100}
        try:
            while True:
                resp = cfg.select_resource_config(**kwargs)
                for s in resp.get("Results", []):
                    items.append(json.loads(s))
                next_token = resp.get("NextToken")
                if not next_token:
                    break
                kwargs["NextToken"] = next_token
            return items
        except Exception as exc:
            msg = str(exc)
            if "not been implemented" in msg or "NotImplemented" in msg:
                return None
            if "InvalidExpression" in msg and not select_fields.startswith(_CONFIG_SELECT_FIELDS_MINIMAL):
                logger.warning(
                    "Espressione Config rifiutata per %s (%s): retry con campi minimi",
                    resource_type, exc,
                )
                extra = ", relationships" if "relationships" in select_fields else ""
                return self._config_query(resource_type, _CONFIG_SELECT_FIELDS_MINIMAL + extra)
            logger.warning("Config query fallita per %s in %s: %s", resource_type, self.region, exc)
            return None

    def _get_tags_by_arn(self, resource_types: list[str]) -> dict[str, dict[str, str]]:
        tagging = self._session.client("resourcegroupstaggingapi", region_name=self.region)
        filters = list({_TAGGING_FILTER[rt] for rt in resource_types if rt in _TAGGING_FILTER})
        result: dict[str, dict[str, str]] = {}
        try:
            paginator = tagging.get_paginator("get_resources")
            page_kwargs: dict = {}
            if filters:
                page_kwargs["ResourceTypeFilters"] = filters
            for page in paginator.paginate(**page_kwargs):
                for r in page.get("ResourceTagMappingList", []):
                    result[r["ResourceARN"]] = {
                        t["Key"]: t["Value"] for t in r.get("Tags", [])
                    }
        except Exception as exc:
            logger.warning("Tagging API non disponibile: %s", exc)
        return result

    # ------------------------------------------------------------------
    # Fallback: describe_* / list_buckets
    # ------------------------------------------------------------------

    def _describe_fallback(
        self, resource_type: str, tags_by_arn: dict[str, dict[str, str]]
    ) -> list[NormalizedResource]:
        handlers = {
            "AWS::EC2::Instance":      self._describe_instances,
            "AWS::EC2::Volume":        self._describe_volumes,
            "AWS::EC2::VPC":           self._describe_vpcs,
            "AWS::EC2::Subnet":        self._describe_subnets,
            "AWS::EC2::SecurityGroup": self._describe_security_groups,
            "AWS::S3::Bucket":         self._list_s3_buckets,
        }
        handler = handlers.get(resource_type)
        if not handler:
            logger.warning("Nessun fallback per %s", resource_type)
            return []
        return handler(tags_by_arn)

    def _ec2(self):
        return self._session.client("ec2", region_name=self.region)

    def _tags_from_list(self, tag_list: list[dict], arn: str, tags_by_arn: dict) -> dict[str, str]:
        live = tags_by_arn.get(arn, {})
        if live:
            return live
        return {t["Key"]: t["Value"] for t in (tag_list or [])}

    def _describe_instances(self, tags_by_arn: dict) -> list[NormalizedResource]:
        ec2 = self._ec2()
        pag = ec2.get_paginator("describe_instances")
        resources: list[NormalizedResource] = []
        for page in pag.paginate():
            for reservation in page["Reservations"]:
                for inst in reservation["Instances"]:
                    iid = inst["InstanceId"]
                    arn = f"arn:aws:ec2:{self.region}:{self.account_id}:instance/{iid}"
                    tags = self._tags_from_list(inst.get("Tags"), arn, tags_by_arn)
                    rels: list[Relationship] = []
                    if inst.get("VpcId"):
                        rels.append(Relationship(
                            type="CONTAINS",
                            target_resource_id=f"arn:aws:ec2:{self.region}:{self.account_id}:vpc/{inst['VpcId']}",
                        ))
                    if inst.get("SubnetId"):
                        rels.append(Relationship(
                            type="CONTAINS",
                            target_resource_id=f"arn:aws:ec2:{self.region}:{self.account_id}:subnet/{inst['SubnetId']}",
                        ))
                    for sg in inst.get("SecurityGroups", []):
                        rels.append(Relationship(
                            type="SECURED_BY",
                            target_resource_id=f"arn:aws:ec2:{self.region}:{self.account_id}:security-group/{sg['GroupId']}",
                        ))
                    resources.append(NormalizedResource(
                        resource_id=arn,
                        account_id=self.account_id,
                        region=self.region,
                        resource_type="AWS::EC2::Instance",
                        current_tags=tags,
                        attributes={
                            "instance_type": inst.get("InstanceType"),
                            "vpc_id": inst.get("VpcId"),
                            "subnet_id": inst.get("SubnetId"),
                            "state": inst.get("State", {}).get("Name"),
                            "image_id": inst.get("ImageId"),
                            "platform": inst.get("Platform", "linux"),
                            "configuration": _jsonable(inst),
                        },
                        relationships=rels,
                    ))
        return resources

    def _describe_volumes(self, tags_by_arn: dict) -> list[NormalizedResource]:
        ec2 = self._ec2()
        pag = ec2.get_paginator("describe_volumes")
        resources: list[NormalizedResource] = []
        for page in pag.paginate():
            for vol in page["Volumes"]:
                vid = vol["VolumeId"]
                arn = f"arn:aws:ec2:{self.region}:{self.account_id}:volume/{vid}"
                tags = self._tags_from_list(vol.get("Tags"), arn, tags_by_arn)
                rels: list[Relationship] = []
                for att in vol.get("Attachments", []):
                    if att.get("InstanceId"):
                        rels.append(Relationship(
                            type="ATTACHED_TO",
                            target_resource_id=f"arn:aws:ec2:{self.region}:{self.account_id}:instance/{att['InstanceId']}",
                        ))
                resources.append(NormalizedResource(
                    resource_id=arn,
                    account_id=self.account_id,
                    region=self.region,
                    resource_type="AWS::EC2::Volume",
                    current_tags=tags,
                    attributes={
                        "size_gb": vol.get("Size"),
                        "volume_type": vol.get("VolumeType"),
                        "state": vol.get("State"),
                        "iops": vol.get("Iops"),
                        "encrypted": vol.get("Encrypted"),
                        "configuration": _jsonable(vol),
                    },
                    relationships=rels,
                ))
        return resources

    def _describe_vpcs(self, tags_by_arn: dict) -> list[NormalizedResource]:
        ec2 = self._ec2()
        pag = ec2.get_paginator("describe_vpcs")
        resources: list[NormalizedResource] = []
        for page in pag.paginate():
            for vpc in page["Vpcs"]:
                vid = vpc["VpcId"]
                arn = f"arn:aws:ec2:{self.region}:{self.account_id}:vpc/{vid}"
                tags = self._tags_from_list(vpc.get("Tags"), arn, tags_by_arn)
                resources.append(NormalizedResource(
                    resource_id=arn,
                    account_id=self.account_id,
                    region=self.region,
                    resource_type="AWS::EC2::VPC",
                    current_tags=tags,
                    attributes={
                        "cidr_block": vpc.get("CidrBlock"),
                        "state": vpc.get("State"),
                        "is_default": vpc.get("IsDefault"),
                        "configuration": _jsonable(vpc),
                    },
                    relationships=[],
                ))
        return resources

    def _describe_subnets(self, tags_by_arn: dict) -> list[NormalizedResource]:
        ec2 = self._ec2()
        pag = ec2.get_paginator("describe_subnets")
        resources: list[NormalizedResource] = []
        for page in pag.paginate():
            for sn in page["Subnets"]:
                sid = sn["SubnetId"]
                arn = f"arn:aws:ec2:{self.region}:{self.account_id}:subnet/{sid}"
                tags = self._tags_from_list(sn.get("Tags"), arn, tags_by_arn)
                rels: list[Relationship] = []
                if sn.get("VpcId"):
                    rels.append(Relationship(
                        type="CONTAINS",
                        target_resource_id=f"arn:aws:ec2:{self.region}:{self.account_id}:vpc/{sn['VpcId']}",
                    ))
                resources.append(NormalizedResource(
                    resource_id=arn,
                    account_id=self.account_id,
                    region=self.region,
                    resource_type="AWS::EC2::Subnet",
                    current_tags=tags,
                    attributes={
                        "cidr_block": sn.get("CidrBlock"),
                        "vpc_id": sn.get("VpcId"),
                        "availability_zone": sn.get("AvailabilityZone"),
                        "configuration": _jsonable(sn),
                    },
                    relationships=rels,
                ))
        return resources

    def _describe_security_groups(self, tags_by_arn: dict) -> list[NormalizedResource]:
        ec2 = self._ec2()
        pag = ec2.get_paginator("describe_security_groups")
        resources: list[NormalizedResource] = []
        for page in pag.paginate():
            for sg in page["SecurityGroups"]:
                gid = sg["GroupId"]
                arn = f"arn:aws:ec2:{self.region}:{self.account_id}:security-group/{gid}"
                tags = self._tags_from_list(sg.get("Tags"), arn, tags_by_arn)
                rels: list[Relationship] = []
                if sg.get("VpcId"):
                    rels.append(Relationship(
                        type="CONTAINS",
                        target_resource_id=f"arn:aws:ec2:{self.region}:{self.account_id}:vpc/{sg['VpcId']}",
                    ))
                resources.append(NormalizedResource(
                    resource_id=arn,
                    account_id=self.account_id,
                    region=self.region,
                    resource_type="AWS::EC2::SecurityGroup",
                    current_tags=tags,
                    attributes={
                        "description": sg.get("Description"),
                        "vpc_id": sg.get("VpcId"),
                        "group_name": sg.get("GroupName"),
                        "configuration": _jsonable(sg),
                    },
                    relationships=rels,
                ))
        return resources

    @staticmethod
    def _bucket_region(s3, bucket: dict) -> Optional[str]:
        region = bucket.get("BucketRegion")
        if region:
            return region
        try:
            loc = s3.get_bucket_location(Bucket=bucket["Name"]).get("LocationConstraint")
        except Exception as exc:
            logger.warning("get_bucket_location fallita per %s: %s", bucket["Name"], exc)
            return None
        # LocationConstraint vuoto = us-east-1; "EU" = alias legacy di eu-west-1
        return {None: "us-east-1", "": "us-east-1", "EU": "eu-west-1"}.get(loc, loc)

    def _list_s3_buckets(self, tags_by_arn: dict) -> list[NormalizedResource]:
        # list_buckets e' globale: teniamo solo i bucket di questa regione,
        # altrimenti in multi-regione ogni bucket comparirebbe N volte.
        # Bucket con regione non determinabile: tenuti (il dedupe per ARN
        # di _per_region evita duplicati).
        s3 = self._session.client("s3", region_name=self.region)
        resp = s3.list_buckets()
        resources: list[NormalizedResource] = []
        for bucket in resp.get("Buckets", []):
            name = bucket["Name"]
            bucket_region = self._bucket_region(s3, bucket)
            if bucket_region and bucket_region != self.region:
                continue
            arn = f"arn:aws:s3:::{name}"
            tags = tags_by_arn.get(arn, {})
            if not tags:
                try:
                    tag_resp = s3.get_bucket_tagging(Bucket=name)
                    tags = {t["Key"]: t["Value"] for t in tag_resp.get("TagSet", [])}
                except Exception:
                    pass
            resources.append(NormalizedResource(
                resource_id=arn,
                account_id=self.account_id,
                region=self.region,
                resource_type="AWS::S3::Bucket",
                current_tags=tags,
                attributes={
                    "creation_date": str(bucket.get("CreationDate", "")),
                    "bucket_name": name,
                    "configuration": _jsonable(bucket),
                },
                relationships=[],
            ))
        return resources

    # ------------------------------------------------------------------
    # Normalize path (from Config items)
    # ------------------------------------------------------------------

    def _normalize(
        self, item: dict, tags_by_arn: dict[str, dict[str, str]]
    ) -> NormalizedResource | None:
        region = item.get("awsRegion") or item.get("region") or self.region
        arn = item.get("arn") or _build_arn(
            item.get("resourceType", ""),
            item.get("resourceId", ""),
            region,
            item.get("accountId", self.account_id),
        )
        if not arn:
            return None

        config_raw = item.get("configuration") or "{}"
        config_data: dict = (
            json.loads(config_raw) if isinstance(config_raw, str) else (config_raw or {})
        )

        live_tags = tags_by_arn.get(arn, {})
        config_tags_raw = item.get("tags") or []
        config_tags: dict[str, str] = (
            {t["key"]: t["value"] for t in config_tags_raw}
            if isinstance(config_tags_raw, list)
            else config_tags_raw
        )
        tags = live_tags if live_tags else config_tags

        resource_type = item.get("resourceType", "")
        account_id = item.get("accountId", self.account_id)

        return NormalizedResource(
            resource_id=arn,
            account_id=account_id,
            region=region,
            resource_type=resource_type,
            current_tags=tags,
            attributes=_build_attributes(resource_type, item, config_data),
            relationships=[
                Relationship(**r)
                for r in _extract_relationships(resource_type, config_data, region, account_id)
            ],
        )


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _build_arn(resource_type: str, resource_id: str, region: str, account_id: str) -> str:
    patterns: dict[str, str] = {
        "AWS::EC2::Instance":      f"arn:aws:ec2:{region}:{account_id}:instance/{resource_id}",
        "AWS::EC2::Volume":        f"arn:aws:ec2:{region}:{account_id}:volume/{resource_id}",
        "AWS::EC2::VPC":           f"arn:aws:ec2:{region}:{account_id}:vpc/{resource_id}",
        "AWS::EC2::Subnet":        f"arn:aws:ec2:{region}:{account_id}:subnet/{resource_id}",
        "AWS::EC2::SecurityGroup": f"arn:aws:ec2:{region}:{account_id}:security-group/{resource_id}",
        "AWS::S3::Bucket":         f"arn:aws:s3:::{resource_id}",
    }
    return patterns.get(resource_type, "")


def _extract_attributes(resource_type: str, config: dict) -> dict:
    if resource_type == "AWS::EC2::Instance":
        state = config.get("state") or {}
        return {
            "instance_type": config.get("instanceType"),
            "vpc_id": config.get("vpcId"),
            "subnet_id": config.get("subnetId"),
            "state": state.get("name") if isinstance(state, dict) else state,
            "image_id": config.get("imageId"),
            "platform": config.get("platform", "linux"),
        }
    if resource_type == "AWS::EC2::Volume":
        return {
            "size_gb": config.get("size"),
            "volume_type": config.get("volumeType"),
            "state": config.get("state"),
            "iops": config.get("iops"),
            "encrypted": config.get("encrypted"),
        }
    if resource_type == "AWS::EC2::VPC":
        return {
            "cidr_block": config.get("cidrBlock"),
            "state": config.get("state"),
            "is_default": config.get("isDefault"),
        }
    if resource_type == "AWS::EC2::Subnet":
        return {
            "cidr_block": config.get("cidrBlock"),
            "vpc_id": config.get("vpcId"),
            "availability_zone": config.get("availabilityZone"),
        }
    if resource_type == "AWS::EC2::SecurityGroup":
        return {
            "description": config.get("description"),
            "vpc_id": config.get("vpcId"),
            "group_name": config.get("groupName"),
        }
    if resource_type == "AWS::S3::Bucket":
        return {
            "creation_date": str(config.get("creationDate", "")),
            "bucket_name": config.get("name"),
        }
    # Tipo non modellato esplicitamente: nessuna chiave normalizzata, la
    # configuration completa viene comunque aggiunta da _build_attributes.
    return {}


def _parse_json_values(data: dict) -> dict:
    """supplementaryConfiguration di Config ha spesso valori JSON-stringa."""
    parsed: dict = {}
    for key, value in (data or {}).items():
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except ValueError:
                pass
        parsed[key] = value
    return parsed


def _build_attributes(resource_type: str, item: dict, config: dict) -> dict:
    """
    Attributi completi di un configuration item di Config. Le chiavi
    normalizzate vanno PER PRIME: agent2 tronca il JSON degli attributi a
    500 caratteri per il prompt, e sono quelle piu' utili all'LLM. Seguono i
    metadati del CI e la configuration integrale (nessuna proprieta' persa).
    """
    attributes = _extract_attributes(resource_type, config)
    metadata = {
        "resource_name": item.get("resourceName"),
        "availability_zone": item.get("availabilityZone"),
        "resource_creation_time": item.get("resourceCreationTime"),
    }
    for key, value in metadata.items():
        if value not in (None, "", "Not Applicable") and key not in attributes:
            attributes[key] = value
    attributes["configuration"] = config
    supplementary = item.get("supplementaryConfiguration")
    if supplementary:
        attributes["supplementary_configuration"] = _parse_json_values(supplementary)
    return attributes


def _extract_relationships(
    resource_type: str, config: dict, region: str, account_id: str
) -> list[dict]:
    rels: list[dict] = []
    if resource_type == "AWS::EC2::Instance":
        if config.get("vpcId"):
            rels.append({"type": "CONTAINS", "target_resource_id": f"arn:aws:ec2:{region}:{account_id}:vpc/{config['vpcId']}"})
        if config.get("subnetId"):
            rels.append({"type": "CONTAINS", "target_resource_id": f"arn:aws:ec2:{region}:{account_id}:subnet/{config['subnetId']}"})
        for sg in config.get("securityGroups", []):
            gid = sg.get("groupId") if isinstance(sg, dict) else sg
            if gid:
                rels.append({"type": "SECURED_BY", "target_resource_id": f"arn:aws:ec2:{region}:{account_id}:security-group/{gid}"})
    elif resource_type in ("AWS::EC2::Subnet", "AWS::EC2::SecurityGroup"):
        if config.get("vpcId"):
            rels.append({"type": "CONTAINS", "target_resource_id": f"arn:aws:ec2:{region}:{account_id}:vpc/{config['vpcId']}"})
    elif resource_type == "AWS::EC2::Volume":
        for att in config.get("attachments", []):
            inst_id = att.get("instanceId") if isinstance(att, dict) else None
            if inst_id:
                rels.append({"type": "ATTACHED_TO", "target_resource_id": f"arn:aws:ec2:{region}:{account_id}:instance/{inst_id}"})
    return rels

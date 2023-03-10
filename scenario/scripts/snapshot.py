import json
import logging
import os
import re
import shlex
import tempfile
from dataclasses import asdict
from enum import Enum
from itertools import chain
from pathlib import Path
from subprocess import CalledProcessError, check_output, run
from textwrap import dedent
from typing import Any, BinaryIO, Dict, Iterable, List, Optional, TextIO, Tuple, Union

import ops.pebble
import typer
import yaml

from scenario.state import (
    Address,
    BindAddress,
    Container,
    DeferredEvent,
    Model,
    Mount,
    Network,
    Relation,
    Secret,
    State,
    Status,
    StoredState,
)

logger = logging.getLogger("snapshot")

JUJU_RELATION_KEYS = frozenset({"egress-subnets", "ingress-address", "private-address"})
JUJU_CONFIG_KEYS = frozenset({})

# TODO: allow passing a custom data dir, else put it in a tempfile in /tmp/.
SNAPSHOT_TEMPDIR_ROOT = (Path(os.getcwd()).parent / "snapshot_storage").absolute()


class SnapshotError(RuntimeError):
    """Base class for errors raised by snapshot."""


class InvalidTargetUnitName(SnapshotError):
    """Raised if the unit name passed to snapshot is invalid."""


class InvalidTargetModelName(SnapshotError):
    """Raised if the model name passed to snapshot is invalid."""


class JujuUnitName(str):
    """This class represents the name of a juju unit that can be snapshotted."""

    def __init__(self, unit_name: str):
        super().__init__()
        app_name, _, unit_id = unit_name.rpartition("/")
        if not app_name or not unit_id:
            raise InvalidTargetUnitName(f"invalid unit name: {unit_name!r}")
        self.unit_name = unit_name
        self.app_name = app_name
        self.unit_id = int(unit_id)
        self.normalized = f"{app_name}-{unit_id}"


def _try_format(string: str):
    try:
        import black

        try:
            return black.format_str(string, mode=black.Mode())
        except black.parsing.InvalidInput as e:
            logger.error(f"error parsing {string}: {e}")
            return string
    except ModuleNotFoundError:
        logger.warning("install black for formatting")
        return string


def format_state(state: State):
    """Pretty-print this State as-is."""
    return _try_format(repr(state))


def format_test_case(state: State, charm_type_name: str = None, event_name: str = None):
    """Format this State as a pytest test case."""
    ct = charm_type_name or "CHARM_TYPE  # TODO: replace with charm type name"
    en = event_name or "EVENT_NAME,  # TODO: replace with event name"
    return _try_format(
        dedent(
            f"""
            from scenario.state import *
            from charm import {ct}
            
            def test_case():
                state = {state}
                out = state.trigger(
                    {en}
                    {ct}
                    )
            
            """
        )
    )


def _juju_run(cmd: str, model=None) -> Dict[str, Any]:
    """Execute juju {command} in a given model."""
    _model = f" -m {model}" if model else ""
    cmd = f"juju {cmd}{_model} --format json"
    raw = run(shlex.split(cmd), capture_output=True).stdout.decode("utf-8")
    return json.loads(raw)


def _juju_ssh(target: JujuUnitName, cmd: str, model: Optional[str] = None) -> str:
    _model = f" -m {model}" if model else ""
    command = f"juju ssh{_model} {target.unit_name} {cmd}"
    raw = run(shlex.split(command), capture_output=True).stdout.decode("utf-8")
    return raw


def _juju_exec(target: JujuUnitName, model: Optional[str], cmd: str) -> str:
    """Execute a juju command.

    Notes:
        Visit the Juju documentation to view all possible Juju commands:
        https://juju.is/docs/olm/juju-cli-commands
    """
    _model = f" -m {model}" if model else ""
    _target = f" -u {target}" if target else ""
    return run(
        shlex.split(f"juju exec{_model}{_target} -- {cmd}"), capture_output=True
    ).stdout.decode("utf-8")


def get_leader(target: JujuUnitName, model: Optional[str]):
    # could also get it from _juju_run('status')...
    logger.info("getting leader...")
    return _juju_exec(target, model, "is-leader") == "True"


def get_network(target: JujuUnitName, model: Optional[str], endpoint: str) -> Network:
    """Get the Network data structure for this endpoint."""
    raw = _juju_exec(target, model, f"network-get {endpoint}")
    jsn = yaml.safe_load(raw)

    bind_addresses = []
    for raw_bind in jsn["bind-addresses"]:

        addresses = []
        for raw_adds in raw_bind["addresses"]:
            addresses.append(
                Address(
                    hostname=raw_adds["hostname"],
                    value=raw_adds["value"],
                    cidr=raw_adds["cidr"],
                    address=raw_adds.get("address", ""),
                )
            )

        bind_addresses.append(
            BindAddress(
                interface_name=raw_bind.get("interface-name", ""), addresses=addresses
            )
        )
    return Network(
        name=endpoint,
        bind_addresses=bind_addresses,
        egress_subnets=jsn.get("egress-subnets", None),
        ingress_addresses=jsn.get("ingress-addresses", None),
    )


def get_secrets(
    target: JujuUnitName,
    model: Optional[str],
    metadata: Dict,
    relations: Tuple[str, ...] = (),
) -> List[Secret]:
    """Get Secret list from the charm."""
    logger.warning("Secrets snapshotting not implemented yet. Also, are you *sure*?")
    return []


def get_stored_state(
    target: JujuUnitName,
    model: Optional[str],
    metadata: Dict,
) -> List[StoredState]:
    """Get StoredState list from the charm."""
    logger.warning("StoredState snapshotting not implemented yet.")
    return []


def get_deferred_events(
    target: JujuUnitName,
    model: Optional[str],
    metadata: Dict,
) -> List[DeferredEvent]:
    """Get DeferredEvent list from the charm."""
    logger.warning("DeferredEvent snapshotting not implemented yet.")
    return []


def get_networks(
    target: JujuUnitName,
    model: Optional[str],
    metadata: Dict,
    include_dead: bool = False,
    relations: Tuple[str, ...] = (),
) -> List[Network]:
    """Get all Networks from this unit."""
    logger.info("getting networks...")
    networks = []
    networks.append(get_network(target, model, "juju-info"))

    endpoints = relations  # only alive relations
    if include_dead:
        endpoints = chain(
            metadata.get("provides", ()),
            metadata.get("requires", ()),
            metadata.get("peers", ()),
        )

    for endpoint in endpoints:
        logger.debug(f"  getting network for endpoint {endpoint!r}")
        networks.append(get_network(target, model, endpoint))
    return networks


def get_metadata(target: JujuUnitName, model: Optional[str]):
    """Get metadata.yaml from this target."""
    logger.info("fetching metadata...")

    raw_meta = _juju_ssh(
        target,
        f"cat ./agents/unit-{target.normalized}/charm/metadata.yaml",
        model=model,
    )
    return yaml.safe_load(raw_meta)


class RemotePebbleClient:
    """Clever little class that wraps calls to a remote pebble client."""

    # TODO: there is a .pebble.state
    #  " j ssh --container traefik traefik/0 cat var/lib/pebble/default/.pebble.state | jq"
    #  figure out what it's for.

    def __init__(
        self, container: str, target: JujuUnitName, model: Optional[str] = None
    ):
        self.socket_path = f"/charm/containers/{container}/pebble.socket"
        self.container = container
        self.target = target
        self.model = model

    def _run(self, cmd: str) -> str:
        _model = f" -m {self.model}" if self.model else ""
        command = f"juju ssh{_model} --container {self.container} {self.target.unit_name} /charm/bin/pebble {cmd}"
        proc = run(shlex.split(command), capture_output=True)
        if proc.returncode == 0:
            return proc.stdout.decode("utf-8")
        raise RuntimeError(
            f"error wrapping pebble call with {command}: "
            f"process exited with {proc.returncode}; "
            f"stdout = {proc.stdout}; "
            f"stderr = {proc.stderr}"
        )

    def can_connect(self) -> bool:
        try:
            version = self.get_system_info()
        except Exception:
            return False
        return bool(version)

    def get_system_info(self):
        return self._run("version")

    def get_plan(self) -> dict:
        plan_raw = self._run("plan")
        return yaml.safe_load(plan_raw)

    def pull(
        self, path: str, *, encoding: Optional[str] = "utf-8"
    ) -> Union[BinaryIO, TextIO]:
        raise NotImplementedError()

    def list_files(
        self, path: str, *, pattern: Optional[str] = None, itself: bool = False
    ) -> List[ops.pebble.FileInfo]:
        raise NotImplementedError()

    def get_checks(
        self,
        level: Optional[ops.pebble.CheckLevel] = None,
        names: Optional[Iterable[str]] = None,
    ) -> List[ops.pebble.CheckInfo]:
        _level = f" --level={level}" if level else ""
        _names = (" " + f" ".join(names)) if names else ""
        out = self._run(f"checks{_level}{_names}")
        if out == "Plan has no health checks.":
            return []
        raise NotImplementedError()


def fetch_file(
    target: JujuUnitName,
    remote_path: str,
    container_name: str,
    local_path: Path = None,
    model: Optional[str] = None,
) -> Optional[str]:
    """Download a file from a live unit to a local path."""
    # copied from jhack
    model_arg = f" -m {model}" if model else ""
    cmd = f"juju ssh --container {container_name}{model_arg} {target.unit_name} cat {remote_path}"
    try:
        raw = check_output(shlex.split(cmd))
    except CalledProcessError as e:
        raise RuntimeError(
            f"Failed to fetch {remote_path} from {target.unit_name}."
        ) from e

    if not local_path:
        return raw.decode("utf-8")

    local_path.write_bytes(raw)


def get_mounts(
    target: JujuUnitName,
    model: Optional[str],
    container_name: str,
    container_meta: Dict,
    fetch_files: Optional[List[Path]] = None,
    temp_dir_base_path: Path = SNAPSHOT_TEMPDIR_ROOT,
) -> Dict[str, Mount]:
    """Get named Mounts from a container's metadata, and download specified files from the target unit."""
    mount_meta = container_meta.get("mounts")

    if fetch_files and not mount_meta:
        logger.error(
            f"No mounts defined for container {container_name} in metadata.yaml. "
            f"Cannot fetch files {fetch_files} for this container."
        )
        return {}

    mount_spec = {}
    for mt in mount_meta:
        if name := mt.get("storage"):
            mount_spec[name] = mt["location"]
        else:
            logger.error(f"unknown mount type: {mt}")

    mounts = {}
    for remote_path in fetch_files or ():
        found = None
        for mn, mt in mount_spec.items():
            if str(remote_path).startswith(mt):
                found = mn, mt

        if not found:
            logger.error(
                f"could not find mount corresponding to requested remote_path {remote_path}: skipping..."
            )
            continue

        mount_name, src = found
        mount = mounts.get(mount_name)
        if not mount:
            # create the mount obj and tempdir
            location = tempfile.TemporaryDirectory(prefix=str(temp_dir_base_path)).name
            mount = Mount(src=src, location=location)
            mounts[mount_name] = mount

        # populate the local tempdir
        filepath = Path(mount.location).joinpath(*remote_path.parts[1:])
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        try:
            fetch_file(
                target,
                container_name=container_name,
                model=model,
                remote_path=remote_path,
                local_path=filepath,
            )

        except RuntimeError as e:
            logger.error(e)

    return mounts


def get_container(
    target: JujuUnitName,
    model: Optional[str],
    container_name: str,
    container_meta: Dict,
    fetch_files: Optional[List[Path]] = None,
    temp_dir_base_path: Path = SNAPSHOT_TEMPDIR_ROOT,
) -> Container:
    """Get container data structure from the target."""
    remote_client = RemotePebbleClient(container_name, target, model)
    plan = remote_client.get_plan()

    return Container(
        name=container_name,
        _base_plan=plan,
        can_connect=remote_client.can_connect(),
        mounts=get_mounts(
            target,
            model,
            container_name,
            container_meta,
            fetch_files,
            temp_dir_base_path=temp_dir_base_path,
        ),
    )


def get_containers(
    target: JujuUnitName,
    model: Optional[str],
    metadata: Optional[Dict],
    fetch_files: Dict[str, List[Path]] = None,
    temp_dir_base_path: Path = SNAPSHOT_TEMPDIR_ROOT,
) -> List[Container]:
    """Get all containers from this unit."""
    fetch_files = fetch_files or {}
    logger.info("getting containers...")

    if not metadata:
        logger.warning("no metadata: unable to get containers")
        return []

    containers = []
    for container_name, container_meta in metadata.get("containers", {}).items():
        container = get_container(
            target,
            model,
            container_name,
            container_meta,
            fetch_files=fetch_files.get(container_name),
            temp_dir_base_path=temp_dir_base_path,
        )
        containers.append(container)
    return containers


def get_status_and_endpoints(
    target: JujuUnitName, model: Optional[str]
) -> Tuple[Status, Tuple[str, ...]]:
    """Parse `juju status` to get the Status data structure and some relation information."""
    logger.info("getting status...")

    status = _juju_run(f"status --relations {target}", model=model)
    app = status["applications"][target.app_name]

    app_status_raw = app["application-status"]
    app_status = app_status_raw["current"], app_status_raw.get("message", "")

    unit_status_raw = app["units"][target]["workload-status"]
    unit_status = unit_status_raw["current"], unit_status_raw.get("message", "")

    relations = tuple(app["relations"].keys())
    app_version = app.get("version", "")
    return Status(app=app_status, unit=unit_status, app_version=app_version), relations


dispatch = {
    "string": str,
    "integer": int,
    "number": float,
    "boolean": lambda x: x == True,
    "attrs": lambda x: x,
}


def get_config(
    target: JujuUnitName, model: Optional[str]
) -> Dict[str, Union[str, int, float, bool]]:
    """Get config dict from target."""

    logger.info("getting config...")
    _model = f" -m {model}" if model else ""
    jsn = _juju_run(f"config {target.app_name}", model=model)

    # dispatch table for builtin config options
    converters = {
        "string": str,
        "integer": int,
        "number": float,
        "boolean": lambda x: x == "true",
        "attrs": lambda x: x,
    }

    cfg = {}
    for name, option in jsn.get("settings", ()).items():
        if value := option.get("value"):
            try:
                converter = converters[option["type"]]
            except KeyError:
                raise ValueError(f'unrecognized type {option["type"]}')
            cfg[name] = converter(value)

        else:
            logger.debug(f"skipped {name}: no value.")

    return cfg


def _get_interface_from_metadata(endpoint: str, metadata: Dict) -> Optional[str]:
    """Get the name of the interface used by endpoint."""
    for role in ["provides", "requires"]:
        for ep, ep_meta in metadata.get(role, {}).items():
            if ep == endpoint:
                return ep_meta["interface"]

    logger.error(f"No interface for endpoint {endpoint} found in charm metadata.")
    return None


def get_relations(
    target: JujuUnitName,
    model: Optional[str],
    metadata: Dict,
    include_juju_relation_data=False,
) -> List[Relation]:
    """Get the list of relations active for this target."""
    logger.info("getting relations...")

    _model = f" -m {model}" if model else ""
    try:
        jsn = _juju_run(f"show-unit {target}", model=model)
    except json.JSONDecodeError as e:
        raise InvalidTargetUnitName(target) from e

    def _clean(relation_data: dict):
        if include_juju_relation_data:
            return relation_data
        else:
            for key in JUJU_RELATION_KEYS:
                del relation_data[key]
        return relation_data

    relations = []
    for raw_relation in jsn[target].get("relation-info", ()):
        logger.debug(
            f"  getting relation data for endpoint {raw_relation.get('endpoint')!r}"
        )
        related_units = raw_relation.get("related-units")
        if not related_units:
            continue
        #    related-units:
        #      owner/0:
        #        in-scope: true
        #        data:
        #          egress-subnets: 10.152.183.130/32
        #          ingress-address: 10.152.183.130
        #          private-address: 10.152.183.130

        relation_id = raw_relation["relation-id"]

        local_unit_data_raw = _juju_exec(
            target, model, f"relation-get -r {relation_id} - {target} --format json"
        )
        local_unit_data = json.loads(local_unit_data_raw)
        local_app_data_raw = _juju_exec(
            target,
            model,
            f"relation-get -r {relation_id} - {target} --format json --app",
        )
        local_app_data = json.loads(local_app_data_raw)

        some_remote_unit_id = JujuUnitName(next(iter(related_units)))
        relations.append(
            Relation(
                endpoint=raw_relation["endpoint"],
                interface=_get_interface_from_metadata(
                    raw_relation["endpoint"], metadata
                ),
                relation_id=relation_id,
                remote_app_data=raw_relation["application-data"],
                remote_app_name=some_remote_unit_id.app_name,
                remote_units_data={
                    JujuUnitName(tgt).unit_id: _clean(val["data"])
                    for tgt, val in related_units.items()
                },
                local_app_data=local_app_data,
                local_unit_data=_clean(local_unit_data),
            )
        )
    return relations


def get_model(name: str = None) -> Model:
    """Get the Model data structure."""
    logger.info("getting model...")

    jsn = _juju_run("models")
    model_name = name or jsn["current-model"]
    try:
        model_info = next(
            filter(lambda m: m["short-name"] == model_name, jsn["models"])
        )
    except StopIteration as e:
        raise InvalidTargetModelName(name) from e

    model_uuid = model_info["model-uuid"]
    model_type = model_info["type"]

    return Model(name=model_name, uuid=model_uuid, type=model_type)


def try_guess_charm_type_name() -> Optional[str]:
    """If we are running this from a charm project root, get the charm type name charm.py is using."""
    try:
        charm_path = Path(os.getcwd()) / "src" / "charm.py"
        if charm_path.exists():
            source = charm_path.read_text()
            charms = re.compile(r"class (\D+)\(CharmBase\):").findall(source)
            if len(charms) < 1:
                raise RuntimeError(f"Not enough charms at {charm_path}.")
            elif len(charms) > 1:
                raise RuntimeError(f"Too many charms at {charm_path}.")
            return charms[0]
    except Exception as e:
        logger.warning(f"unable to guess charm type: {e}")
    return None


class FormatOption(
    str, Enum
):  # Enum for typer support, str for native comparison and ==.
    """Output formatting options for snapshot."""

    state = "state"  # the default: will print the python repr of the State dataclass.
    json = "json"
    pytest = "pytest"


def _snapshot(
    target: str,
    model: Optional[str] = None,
    pprint: bool = True,
    include: str = None,
    include_juju_relation_data=False,
    include_dead_relation_networks=False,
    format: FormatOption = "state",
    fetch_files: Dict[str, List[Path]] = None,
    temp_dir_base_path: Path = SNAPSHOT_TEMPDIR_ROOT,
):
    """see snapshot's docstring"""
    try:
        target = JujuUnitName(target)
    except InvalidTargetUnitName:
        logger.critical(
            f"invalid target: {target!r} is not a valid unit name. Should be formatted like so:"
            f"`foo/1`, or `database/0`, or `myapp-foo-bar/42`."
        )
        exit(1)

    logger.info(f'beginning snapshot of {target} in model {model or "<current>"}...')

    def ifinclude(key, get_value, null_value):
        if include is None or key in include:
            return get_value()
        return null_value

    metadata = get_metadata(target, model)
    if not metadata:
        logger.critical(f"could not fetch metadata from {target}.")
        exit(1)

    try:
        status, endpoints = get_status_and_endpoints(target, model)
        state = State(
            juju_version=get_juju_version(),
            unit_id=target.unit_id,
            app_name=target.app_name,
            leader=get_leader(target, model),
            model=get_model(model),
            status=status,
            config=ifinclude("c", lambda: get_config(target, model), {}),
            relations=ifinclude(
                "r",
                lambda: get_relations(
                    target,
                    model,
                    metadata=metadata,
                    include_juju_relation_data=include_juju_relation_data,
                ),
                [],
            ),
            containers=ifinclude(
                "k",
                lambda: get_containers(
                    target,
                    model,
                    metadata,
                    fetch_files=fetch_files,
                    temp_dir_base_path=temp_dir_base_path,
                ),
                [],
            ),
            networks=ifinclude(
                "n",
                lambda: get_networks(
                    target,
                    model,
                    metadata,
                    include_dead=include_dead_relation_networks,
                    relations=endpoints,
                ),
                [],
            ),
            secrets=ifinclude(
                "s",
                lambda: get_secrets(
                    target,
                    model,
                    metadata,
                    relations=endpoints,
                ),
                [],
            ),
            deferred=ifinclude(
                "d",
                lambda: get_deferred_events(
                    target,
                    model,
                    metadata,
                ),
                [],
            ),
            stored_state=ifinclude(
                "t",
                lambda: get_stored_state(
                    target,
                    model,
                    metadata,
                ),
                [],
            ),
        )

        # todo: these errors should surface earlier.
    except InvalidTargetUnitName:
        _model = f"model {model}" or "the current model"
        logger.critical(f"invalid target: {target!r} not found in {_model}")
        exit(1)
    except InvalidTargetModelName:
        logger.critical(f"invalid model: {model!r} not found.")
        exit(1)

    logger.info(f"snapshot done.")

    if pprint:
        if format == FormatOption.pytest:
            charm_type_name = try_guess_charm_type_name()
            txt = format_test_case(state, charm_type_name=charm_type_name)
        elif format == FormatOption.state:
            txt = format_state(state)
        elif format == FormatOption.json:
            txt = json.dumps(asdict(state), indent=2)
        else:
            raise ValueError(f"unknown format {format}")

        print(txt)

    return state


def snapshot(
    target: str = typer.Argument(..., help="Target unit."),
    model: Optional[str] = typer.Option(
        None, "-m", "--model", help="Which model to look at."
    ),
    format: FormatOption = typer.Option(
        "state",
        "-f",
        "--format",
        help="How to format the output. "
        "``state``: Outputs a black-formatted repr() of the State object (if black is installed! "
        "else it will be ugly but valid python code). "
        "``json``: Outputs a Jsonified State object. Perfect for storage. "
        "``pytest``: Outputs a full-blown pytest scenario test based on this State. "
        "Pipe it to a file and fill in the blanks.",
    ),
    include: str = typer.Option(
        "rckn",
        "--include",
        "-i",
        help="What data to include in the state. "
        "``r``: relation, ``c``: config, ``k``: containers, ``n``: networks, ``s``: secrets(!).",
    ),
    include_dead_relation_networks: bool = typer.Option(
        False,
        "--include-dead-relation-networks",
        help="Whether to gather networks of inactive relation endpoints.",
        is_flag=True,
    ),
    include_juju_relation_data: bool = typer.Option(
        False,
        "--include-juju-relation-data",
        help="Whether to include in the relation data the default juju keys (egress-subnets,"
        "ingress-address, private-address).",
        is_flag=True,
    ),
) -> State:
    """Gather and output the State of a remote target unit.

    If black is available, the output will be piped through it for formatting.

    Usage: snapshot myapp/0 > ./tests/scenario/case1.py
    """
    return _snapshot(
        target=target,
        model=model,
        format=format,
        include=include,
        include_juju_relation_data=include_juju_relation_data,
        include_dead_relation_networks=include_dead_relation_networks,
    )


# for the benefit of script usage
_snapshot.__doc__ = snapshot.__doc__

if __name__ == "__main__":
    print(_snapshot("prom/0", model="foo", format=FormatOption.pytest))

    # print(
    #         _snapshot(
    #             "traefik/0",
    #             model="cos",
    #             format=FormatOption.json,
    #             fetch_files={
    #                 "traefik": [
    #                     Path("/opt/traefik/juju/certificates.yaml"),
    #                     Path("/opt/traefik/juju/certificate.cert"),
    #                     Path("/opt/traefik/juju/certificate.key"),
    #                     Path("/etc/traefik/traefik.yaml"),
    #                 ]
    #             },
    #         )
    #     )

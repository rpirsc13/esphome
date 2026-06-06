import hashlib
import logging
import re
from pathlib import Path
import urllib.parse

from esphome import external_files
import esphome.codegen as cg
from esphome.components import esp32, i2c
import esphome.config_validation as cv
from esphome.const import (
    CONF_ID,
    CONF_SAMPLE_RATE,
    CONF_TEMPERATURE_OFFSET,
    Framework,
)
from esphome.core import CORE, EsphomeError

_LOGGER = logging.getLogger(__name__)

DEPENDENCIES = ["i2c"]
AUTO_LOAD = ["sensor", "text_sensor"]
MULTI_CONF = True

DOMAIN = "bme690"

CONF_BME690_ID = "bme690_id"
CONF_BSEC_LIBRARY = "bsec_library"
CONF_BSEC_CONFIG = "bsec_config"
CONF_BSEC_CONFIG_DATA_ID = "bsec_config_data_id"
CONF_STATE_SAVE_INTERVAL = "state_save_interval"
CONF_SUPPLY_VOLTAGE = "supply_voltage"

bme690_ns = cg.esphome_ns.namespace("bme690")
BME690Component = bme690_ns.class_(
    "BME690Component", cg.PollingComponent, i2c.I2CDevice
)

SampleRate = bme690_ns.enum("SampleRate")
SAMPLE_RATE_OPTIONS = {
    "LP": SampleRate.SAMPLE_RATE_LP,
    "ULP": SampleRate.SAMPLE_RATE_ULP,
}

SupplyVoltage = bme690_ns.enum("SupplyVoltage")
SUPPLY_VOLTAGE_OPTIONS = {
    "1.8V": SupplyVoltage.SUPPLY_VOLTAGE_1V8,
    "3.3V": SupplyVoltage.SUPPLY_VOLTAGE_3V3,
}


def _compute_local_file_path(url: str, *, prefix: str) -> Path:
    h = hashlib.new("sha256")
    h.update(url.encode())
    key = h.hexdigest()[:8]
    base_dir = external_files.compute_local_file_dir(DOMAIN)
    return base_dir / f"{prefix}_{key}"


def _resolve_bsec_library(value: Path | str) -> Path:
    if isinstance(value, Path):
        return value

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "file":
        file_path = Path(parsed.path)
        if not file_path.is_file():
            raise cv.Invalid(f"Could not find file '{file_path}'")
        return file_path

    path = _compute_local_file_path(value, prefix="bsec")
    external_files.download_content(value, path)
    return path


def _resolve_bsec_config(value: Path | str) -> Path:
    if isinstance(value, Path):
        return value

    parsed = urllib.parse.urlparse(value)
    if parsed.scheme == "file":
        file_path = Path(parsed.path)
        if not file_path.is_file():
            raise cv.Invalid(f"Could not find file '{file_path}'")
        return file_path

    path = _compute_local_file_path(value, prefix="bsec_config")
    external_files.download_content(value, path)
    return path


def _strip_config_preamble(text: str) -> str:
    lines = []
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("#") or stripped.startswith("//"):
            continue
        lines.append(stripped)
    if not lines:
        return ""

    # Some exports prefix the blob with a standalone array length (e.g. "550").
    first = lines[0]
    if re.fullmatch(r"\d+", first) and int(first) > 255:
        _LOGGER.warning(
            "Skipping leading BSEC config length marker '%s' (not a byte value)",
            first,
        )
        lines = lines[1:]

    return "\n".join(lines)


def _parse_comma_separated_bytes(text: str) -> list[int]:
    values = []
    first_token = True
    for part in text.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            value = int(part, 0)
        except ValueError as err:
            msg = f"invalid BSEC config byte '{part}'"
            raise ValueError(msg) from err

        if first_token and value > 255:
            _LOGGER.warning(
                "Skipping leading BSEC config length marker '%s' (not a byte value)",
                value,
            )
            first_token = False
            continue

        first_token = False
        if not 0 <= value <= 255:
            msg = (
                f"BSEC config byte value {value} is out of range 0-255. "
                "If this is the array length from a Bosch .c file, remove it and keep "
                "only the comma-separated byte values."
            )
            raise ValueError(msg)
        values.append(value)

    if not values:
        raise ValueError("BSEC config contains no byte values")

    return values


def _load_bsec_config_bytes(path: Path) -> list[int]:
    raw = path.read_bytes()
    text = _strip_config_preamble(raw.decode("utf-8-sig", errors="ignore").strip())

    # C source from Bosch SDK: const uint8_t bsec_config_iaq[N] = { ... };
    brace_start = text.find("{")
    brace_end = text.rfind("}")
    if brace_start != -1 and brace_end > brace_start:
        return _parse_comma_separated_bytes(text[brace_start + 1 : brace_end])

    # Comma-separated .txt/.csv blob (BSEC2-style export)
    if "," in text:
        return _parse_comma_separated_bytes(text)

    # Whitespace-separated decimal bytes
    if re.fullmatch(r"[\d\s]+", text):
        return _parse_comma_separated_bytes(text.replace("\n", ","))

    # Raw binary blob
    return list(raw)


def _validate_bme690(config):
    if CONF_BSEC_CONFIG not in config and (
        config[CONF_SUPPLY_VOLTAGE] != "1.8V" or config[CONF_SAMPLE_RATE] != "ULP"
    ):
        _LOGGER.warning(
            "%s: built-in BSEC config targets generic_18v_300s_28d (1.8V, ULP). "
            "For supply_voltage=%s and sample_rate=%s, set %s to the matching file "
            "from the Bosch BSEC v3 package.",
            DOMAIN,
            config[CONF_SUPPLY_VOLTAGE],
            config[CONF_SAMPLE_RATE],
            CONF_BSEC_CONFIG,
        )
    return config


CONFIG_SCHEMA = cv.All(
    cv.Schema(
        {
            cv.GenerateID(): cv.declare_id(BME690Component),
            cv.Required(CONF_BSEC_LIBRARY): cv.Any(cv.file_, cv.url),
            cv.Optional(CONF_BSEC_CONFIG): cv.Any(cv.file_, cv.url),
            cv.GenerateID(CONF_BSEC_CONFIG_DATA_ID): cv.declare_id(cg.uint8),
            cv.Optional(CONF_TEMPERATURE_OFFSET, default=0): cv.temperature_delta,
            cv.Optional(CONF_SAMPLE_RATE, default="ULP"): cv.enum(
                SAMPLE_RATE_OPTIONS, upper=True
            ),
            cv.Optional(CONF_SUPPLY_VOLTAGE, default="1.8V"): cv.enum(
                SUPPLY_VOLTAGE_OPTIONS, upper=True
            ),
            cv.Optional(
                CONF_STATE_SAVE_INTERVAL, default="6hours"
            ): cv.positive_time_period_minutes,
        }
    )
    .extend(cv.polling_component_schema("5s"))
    .extend(i2c.i2c_device_schema(0x76)),
    cv.only_with_framework(
        frameworks=Framework.ESP_IDF,
        suggestions={
            Framework.ARDUINO: ("bme680", "bme68x_bsec2"),
        },
    ),
    cv.All(
        cv.only_on_esp32,
        esp32.only_on_variant(
            supported=[esp32.VARIANT_ESP32C6, esp32.VARIANT_ESP32S3]
        ),
    ),
    _validate_bme690,
)


async def to_code(config):
    var = cg.new_Pvariable(config[CONF_ID])
    await cg.register_component(var, config)
    await i2c.register_i2c_device(var, config)

    lib_path = _resolve_bsec_library(config[CONF_BSEC_LIBRARY])
    esp32.add_extra_build_file("libalgobsec.a", lib_path)

    build_dir = CORE.relative_build_path()
    cg.add_build_flag(
        f"-L{build_dir} -Wl,--whole-archive -lalgobsec -Wl,--no-whole-archive"
    )

    cg.add(var.set_temperature_offset(config[CONF_TEMPERATURE_OFFSET]))
    cg.add(var.set_sample_rate(config[CONF_SAMPLE_RATE]))
    cg.add(var.set_supply_voltage(config[CONF_SUPPLY_VOLTAGE]))
    cg.add(
        var.set_state_save_interval(config[CONF_STATE_SAVE_INTERVAL].total_milliseconds)
    )

    if CONF_BSEC_CONFIG in config:
        config_path = _resolve_bsec_config(config[CONF_BSEC_CONFIG])
        try:
            config_bytes = _load_bsec_config_bytes(config_path)
        except (OSError, ValueError) as err:
            raise EsphomeError(
                f"Could not read BSEC config file '{config_path}': {err}"
            ) from err
        if not config_bytes:
            raise EsphomeError(f"BSEC config file '{config_path}' is empty")
        config_arr = cg.static_const_array(
            config[CONF_BSEC_CONFIG_DATA_ID],
            cg.ArrayInitializer(*config_bytes, multiline=True),
        )
        cg.add(var.set_bsec_configuration(config_arr, len(config_bytes)))
    else:
        cg.add(
            var.set_bsec_configuration(
                cg.RawExpression("bsec_config_iaq"),
                cg.RawExpression("sizeof(bsec_config_iaq)"),
            )
        )

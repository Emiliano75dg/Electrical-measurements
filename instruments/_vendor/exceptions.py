"""Custom exception hierarchy for the electrical_measurements package."""


class ElectricalMeasurementsError(Exception):
    """Base exception for package-specific failures."""


class InstrumentError(ElectricalMeasurementsError):
    """Base exception for instrument-related failures."""


class InstrumentConfigError(InstrumentError):
    """Raised when instrument configuration is invalid or unsupported."""


class InstrumentConnectionError(InstrumentError):
    """Raised when an instrument connection cannot be established."""


class InstrumentTimeoutError(InstrumentError):
    """Raised when an instrument operation exceeds its timeout."""


class HardwareError(InstrumentError):
    """Raised when a hardware command or readback fails."""


class EnvironmentControlError(InstrumentError):
    """Raised when environment control is unavailable for the current mode."""


class ContactMapError(ElectricalMeasurementsError):
    """Raised when a contact map file or state definition is invalid."""


class MatrixSwitchError(ElectricalMeasurementsError):
    """Raised when the relay matrix cannot apply a requested state safely."""


class ProtocolConfigError(ElectricalMeasurementsError):
    """Raised when a measurement protocol is configured with invalid inputs."""


class RunnerInputError(ElectricalMeasurementsError):
    """Raised when CLI or runner inputs are invalid or inconsistent."""


class SequenceValidationError(ElectricalMeasurementsError):
    """Raised when a measurement sequence file is invalid or inconsistent."""

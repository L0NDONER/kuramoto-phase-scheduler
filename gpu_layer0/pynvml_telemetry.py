"""pynvml_telemetry.py — fast in-process GPU telemetry sampling.

Uses pynvml (NVML bindings) directly, not the nvidia-smi CLI: spawning
a subprocess per sample costs ~50-100ms, which is too slow relative to
layer0_oscillator.py's carrier period (tens of ms) -- same aliasing
risk as stepping RK4 at or above the thing it's integrating.

Usage:
    pip install pynvml   # first cell, on Kaggle or anywhere else
    from pynvml_telemetry import GpuTelemetry
    t = GpuTelemetry()
    for sample in t.sample_all():
        print(sample)
"""
from dataclasses import dataclass


@dataclass
class GpuSample:
    index: int
    name: str
    power_w: float
    power_limit_w: float
    temp_c: float
    sm_clock_mhz: float
    util_pct: float


class GpuTelemetry:
    def __init__(self):
        import pynvml
        self._nvml = pynvml
        self._nvml.nvmlInit()
        self.n_gpus = self._nvml.nvmlDeviceGetCount()
        self._handles = [self._nvml.nvmlDeviceGetHandleByIndex(i)
                          for i in range(self.n_gpus)]
        if self.n_gpus == 0:
            raise RuntimeError("no NVML-visible GPUs -- on Kaggle, check "
                                "Settings -> Accelerator is set to a GPU")

    def sample_all(self):
        return [self._sample_one(i, h) for i, h in enumerate(self._handles)]

    def _sample_one(self, index, handle):
        nvml = self._nvml
        name = nvml.nvmlDeviceGetName(handle)
        if isinstance(name, bytes):
            name = name.decode("utf-8", "replace")

        power_w = nvml.nvmlDeviceGetPowerUsage(handle) / 1000.0
        try:
            power_limit_w = nvml.nvmlDeviceGetEnforcedPowerLimit(handle) / 1000.0
        except Exception:
            power_limit_w = nvml.nvmlDeviceGetPowerManagementLimit(handle) / 1000.0
        temp_c = nvml.nvmlDeviceGetTemperature(handle, nvml.NVML_TEMPERATURE_GPU)
        sm_clock_mhz = nvml.nvmlDeviceGetClockInfo(handle, nvml.NVML_CLOCK_SM)
        util_pct = nvml.nvmlDeviceGetUtilizationRates(handle).gpu

        return GpuSample(index=index, name=name, power_w=power_w,
                          power_limit_w=power_limit_w, temp_c=temp_c,
                          sm_clock_mhz=sm_clock_mhz, util_pct=util_pct)

    def shutdown(self):
        self._nvml.nvmlShutdown()

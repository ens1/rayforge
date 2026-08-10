"""Tests for the core PWM settings varset."""

import pytest

from rayforge.core.varset import FloatVar, IntVar, VarSet
from rayforge.machine.driver.driver import PWMParams, pwm_varset


def test_construction():
    vs = pwm_varset(
        PWMParams(
            frequency=1000,
            max_frequency=5000,
            pulse_width=50,
            min_pulse_width=1,
            max_pulse_width=100,
        )
    )
    assert isinstance(vs, VarSet)
    keys = [v.key for v in vs]
    assert "frequency" in keys
    assert "pulse_width" in keys


def test_defaults():
    vs = pwm_varset(PWMParams(1000, 5000, 50, 1, 100))
    freq_var = vs["frequency"]
    assert isinstance(freq_var, IntVar)
    assert freq_var.default == 1000
    pulse_var = vs["pulse_width"]
    assert isinstance(pulse_var, FloatVar)
    assert pulse_var.default == 50


def test_bounds():
    vs = pwm_varset(PWMParams(1000, 5000, 50, 1, 100))
    freq_var = vs["frequency"]
    assert isinstance(freq_var, IntVar)
    assert freq_var.min_val == 1
    assert freq_var.max_val == 5000
    pulse_var = vs["pulse_width"]
    assert isinstance(pulse_var, FloatVar)
    assert pulse_var.min_val == 1
    assert pulse_var.max_val == 100


def test_independent_pwm_fields():
    frequency_only = pwm_varset(
        PWMParams(10_000, 20_000, None, None, None, 10_000)
    )
    pulse_only = pwm_varset(PWMParams(None, None, 0.1, 0.0, 0.2, None))

    assert [var.key for var in frequency_only] == ["frequency"]
    frequency = frequency_only["frequency"]
    assert isinstance(frequency, IntVar)
    assert frequency.min_val == 10_000
    assert [var.key for var in pulse_only] == ["pulse_width"]
    pulse = pulse_only["pulse_width"]
    assert isinstance(pulse, FloatVar)
    assert pulse.default == pytest.approx(0.1)


def test_frequency_zero_disable_sentinel_preserves_nonzero_bounds():
    params = PWMParams(
        frequency=20_000,
        min_frequency=10_000,
        max_frequency=20_000,
        frequency_zero_disables=True,
        pulse_width=None,
        min_pulse_width=None,
        max_pulse_width=None,
    )

    frequency = pwm_varset(params)["frequency"]

    assert isinstance(frequency, IntVar)
    assert params.min_frequency == 10_000
    assert frequency.min_val == 0
    assert frequency.max_val == 20_000
    assert frequency.description == (
        "0 disables; nonzero must be 10000–20000 Hz"
    )

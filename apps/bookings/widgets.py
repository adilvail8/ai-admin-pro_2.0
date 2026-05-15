"""Custom form widgets for the bookings admin.

Currently a single widget — ``WorkingHoursWidget`` — that replaces the
raw JSON ``<textarea>`` for ``Master.working_hours`` with a 7-row table
of [check] [time start] [time end] inputs, one row per weekday.
"""

from django import forms
from django.utils.safestring import mark_safe

from .models import WEEKDAY_KEYS


WEEKDAY_LABELS = {
    "mon": "Пн",
    "tue": "Вт",
    "wed": "Ср",
    "thu": "Чт",
    "fri": "Пт",
    "sat": "Сб",
    "sun": "Вс",
}


_TOGGLE_JS = (
    "var row=this.closest('tr');"
    "row.querySelector('input[data-role=start]').disabled=!this.checked;"
    "row.querySelector('input[data-role=end]').disabled=!this.checked;"
)


class WorkingHoursWidget(forms.Widget):
    """Render Master.working_hours as a weekly schedule table.

    Stored shape: ``{"mon": {"start": "09:00", "end": "18:00"}, ...}``
    Days where the master is off are simply absent from the dict — same
    semantics ``Master.get_daily_schedule`` and ``iter_master_slots``
    already expect, so no model migration is needed.
    """

    template_name = None  # rendered inline, no external template

    def value_from_datadict(self, data, files, name):
        result = {}
        for key in WEEKDAY_KEYS:
            if not data.get(f"{name}_{key}_active"):
                continue
            start = (data.get(f"{name}_{key}_start") or "").strip()
            end = (data.get(f"{name}_{key}_end") or "").strip()
            if not start or not end:
                continue
            result[key] = {"start": start, "end": end}
        return result

    def render(self, name, value, attrs=None, renderer=None):
        if not isinstance(value, dict):
            value = {}
        rows = []
        for key in WEEKDAY_KEYS:
            label = WEEKDAY_LABELS[key]
            day = value.get(key)
            active = isinstance(day, dict) and bool(day)
            start = day.get("start", "09:00") if active else "09:00"
            end = day.get("end", "18:00") if active else "18:00"
            checked = "checked" if active else ""
            disabled = "" if active else "disabled"
            rows.append(
                f"""
                <tr>
                  <td style="padding: 6px 14px 6px 0; font-weight: 500; color: #374151;">{label}</td>
                  <td style="padding: 6px 14px 6px 0;">
                    <input type="checkbox"
                           name="{name}_{key}_active"
                           {checked}
                           onchange="{_TOGGLE_JS}">
                  </td>
                  <td style="padding: 6px 8px 6px 0;">
                    <input type="time"
                           name="{name}_{key}_start"
                           data-role="start"
                           value="{start}"
                           step="900"
                           {disabled}
                           style="padding: 4px 6px;">
                  </td>
                  <td style="padding: 6px 0;">
                    <input type="time"
                           name="{name}_{key}_end"
                           data-role="end"
                           value="{end}"
                           step="900"
                           {disabled}
                           style="padding: 4px 6px;">
                  </td>
                </tr>
                """
            )
        rows_html = "".join(rows)
        return mark_safe(
            f"""
            <table class="working-hours-widget" style="border-collapse: collapse; margin-top: 4px;">
              <thead>
                <tr>
                  <th style="text-align: left; padding: 4px 14px 4px 0; font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em;">День</th>
                  <th style="text-align: left; padding: 4px 14px 4px 0; font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em;">Работает</th>
                  <th style="text-align: left; padding: 4px 8px 4px 0; font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em;">С</th>
                  <th style="text-align: left; padding: 4px 0; font-size: 11px; color: #6b7280; text-transform: uppercase; letter-spacing: 0.05em;">До</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            <p style="margin: 6px 0 0 0; font-size: 11px; color: #9ca3af;">
              Снимите галочку, чтобы пометить день как выходной.
            </p>
            """
        )

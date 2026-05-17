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
        # Tailwind utility classes (Unfold подгружает) — тёмная тема ловится
        # через `dark:` варианты. `accent-color` + `color-scheme: light dark`
        # на input'ах гарантирует, что нативные checkbox/time контролы не
        # сливаются с тёмным фоном Unfold (баг был: галочки невидимы).
        day_cell = "py-1.5 pr-3.5 font-medium text-base-700 dark:text-base-200"
        action_cell = "py-1.5 pr-3.5"
        time_cell_start = "py-1.5 pr-2"
        time_cell_end = "py-1.5"
        time_input_class = (
            "px-1.5 py-0.5 rounded border border-base-200 dark:border-base-700 "
            "bg-white dark:bg-base-900 text-base-900 dark:text-base-100"
        )
        time_input_style = "accent-color: rgb(37 99 235); color-scheme: light dark;"
        checkbox_style = (
            "accent-color: rgb(37 99 235); color-scheme: light dark; "
            "width: 16px; height: 16px;"
        )
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
                  <td class="{day_cell}">{label}</td>
                  <td class="{action_cell}">
                    <input type="checkbox"
                           name="{name}_{key}_active"
                           style="{checkbox_style}"
                           {checked}
                           onchange="{_TOGGLE_JS}">
                  </td>
                  <td class="{time_cell_start}">
                    <input type="time"
                           name="{name}_{key}_start"
                           data-role="start"
                           value="{start}"
                           step="900"
                           {disabled}
                           class="{time_input_class}"
                           style="{time_input_style}">
                  </td>
                  <td class="{time_cell_end}">
                    <input type="time"
                           name="{name}_{key}_end"
                           data-role="end"
                           value="{end}"
                           step="900"
                           {disabled}
                           class="{time_input_class}"
                           style="{time_input_style}">
                  </td>
                </tr>
                """
            )
        rows_html = "".join(rows)
        header_cell = (
            "text-left py-1 pr-3.5 text-[11px] uppercase tracking-wider "
            "text-base-500 dark:text-base-400 font-medium"
        )
        return mark_safe(
            f"""
            <table class="working-hours-widget mt-1 border-collapse">
              <thead>
                <tr>
                  <th class="{header_cell}">День</th>
                  <th class="{header_cell}">Работает</th>
                  <th class="{header_cell}">С</th>
                  <th class="{header_cell}">До</th>
                </tr>
              </thead>
              <tbody>{rows_html}</tbody>
            </table>
            <p class="mt-1.5 text-[11px] text-base-400 dark:text-base-500">
              Снимите галочку, чтобы пометить день как выходной.
            </p>
            """
        )

"""Custom form widgets for the bookings admin.

- ``WorkingHoursWidget`` replaces the raw JSON ``<textarea>`` for
  ``Master.working_hours`` with a 7-row table of weekday rows.
- ``DurationHoursMinutesWidget`` replaces Django's stock
  ``HH:MM:SS`` text input on ``DurationField`` with two number inputs
  (hours / minutes) — much more obvious for a salon owner.
"""

from datetime import timedelta

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


class DurationHoursMinutesWidget(forms.Widget):
    """Render a ``DurationField`` as two number inputs: hours + minutes.

    Django's default DurationField widget is a plain ``<input type=text>``
    expecting ``HH:MM:SS`` — unintuitive for a salon owner who just wants
    to say "45 минут" or "1 час 30 минут". This widget collects two
    separate ints and packs them back into the canonical
    ``HH:MM:SS`` string that DurationField.to_python accepts.

    Empty / zero is allowed — useful for ``buffer_time`` where 0 minutes
    is a valid value meaning "no buffer needed".
    """

    template_name = None  # rendered inline, no external template

    def value_from_datadict(self, data, files, name):
        hours_raw = (data.get(f"{name}_hours") or "0").strip() or "0"
        minutes_raw = (data.get(f"{name}_minutes") or "0").strip() or "0"
        try:
            hours = max(int(hours_raw), 0)
            minutes = max(int(minutes_raw), 0)
        except (TypeError, ValueError):
            # Bad input — let DurationField raise a validation error on ""
            return ""
        total_seconds = hours * 3600 + minutes * 60
        # DurationField.to_python accepts "HH:MM:SS" exactly.
        td = timedelta(seconds=total_seconds)
        # Normalise: split into days/hours/minutes/seconds for HH:MM:SS.
        total_minutes, secs = divmod(int(td.total_seconds()), 60)
        h, m = divmod(total_minutes, 60)
        return f"{h:02d}:{m:02d}:{secs:02d}"

    def _decompose(self, value):
        """Extract (hours, minutes) for the bound input.

        ``value`` may arrive as a ``timedelta`` (from model.refresh) or
        a string ("HH:MM:SS" — from form rebind after validation error).
        """
        if value is None or value == "":
            return 0, 0
        if isinstance(value, timedelta):
            total_minutes = int(value.total_seconds() // 60)
        else:
            try:
                parts = str(value).split(":")
                total_minutes = int(parts[0]) * 60 + int(parts[1])
            except (ValueError, IndexError):
                total_minutes = 0
        if total_minutes < 0:
            total_minutes = 0
        return divmod(total_minutes, 60)

    def render(self, name, value, attrs=None, renderer=None):
        hours, minutes = self._decompose(value)
        input_class = (
            "px-2 py-1 rounded border border-base-200 dark:border-base-700 "
            "bg-white dark:bg-base-900 text-base-900 dark:text-base-100 w-20"
        )
        input_style = "accent-color: rgb(37 99 235); color-scheme: light dark;"
        label_class = "text-base-500 dark:text-base-400 text-[13px]"
        return mark_safe(
            f"""
            <span class="inline-flex items-center gap-2"
                  style="font-variant-numeric: tabular-nums;">
              <input type="number"
                     name="{name}_hours"
                     value="{hours}"
                     min="0"
                     step="1"
                     class="{input_class}"
                     style="{input_style}">
              <span class="{label_class}">ч</span>
              <input type="number"
                     name="{name}_minutes"
                     value="{minutes}"
                     min="0"
                     max="59"
                     step="5"
                     class="{input_class}"
                     style="{input_style}">
              <span class="{label_class}">мин</span>
            </span>
            """
        )

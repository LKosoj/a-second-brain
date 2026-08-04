#!/usr/bin/env python3
"""
MPP Writer — генерация MS Project XML (.mspdi / .xml) из JSON данных.

Универсальный скрипт для создания/редактирования планов проектов в формате MS Project.
Читает JSON (формат mpp_read.py), генерирует валидный MSPDI XML.

Полная поддержка: задачи, ресурсы, назначения, WBS, иерархия, вехи.

Использование:
  python3 mpp_write.py input.json --output project.xml [--corrections corrections.json]

Структура corrections.json:
{
  "task_corrections": {
    "task_name_or_id": {"duration": 10, "start": "2026-05-01", "note": "+2д буфер"}
  },
  "project_notes": "Дополнительные заметки к проекту"
}
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom


def prettify_xml(elem):
    """Возвращает отформатированный XML с отступами."""
    rough_string = tostring(elem, encoding='unicode')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="  ")


def parse_duration(dur_str):
    """Парсит длительность вида '10.0d' -> 10 (дней)."""
    if not dur_str:
        return 1
    match = re.match(r'(\d+(?:\.\d+)?)\s*([dwhm]?)', dur_str.lower())
    if match:
        val = float(match.group(1))
        unit = match.group(2)
        if unit == 'h':
            return max(1, int(val / 8))
        elif unit == 'w':
            return int(val * 5)
        elif unit == 'm':
            return int(val * 20)
        else:
            return int(val)
    return 1


def format_duration(days):
    """Форматирует длительность в формат PT[N]H0M0S (часы для MS Project)."""
    hours = days * 8
    return f"PT{hours}H0M0S"


def parse_date(date_str):
    """Парсит дату из строки вида '2026-04-01T09:00'."""
    if not date_str:
        return None
    try:
        return datetime.strptime(date_str[:19], "%Y-%m-%dT%H:%M")
    except ValueError:
        try:
            return datetime.strptime(date_str[:10], "%Y-%m-%d")
        except ValueError:
            return None


def format_date(dt):
    """Форматирует дату для MSPDI."""
    if dt is None:
        return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def add_workdays(start, days):
    """Добавляет рабочие дни (пн-пт) к дате."""
    if days <= 0:
        return start
    current = start
    added = 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5:
            added += 1
    return current


def build_task_index(tasks):
    """Строит индексы задач по имени, ID и uniqueID."""
    by_name = {}
    by_id = {}
    by_uid = {}
    
    for i, task in enumerate(tasks):
        name = task.get("name", "")
        task_id = task.get("id") or task.get("ID") or (i + 1)
        uid = task.get("unique_id") or task.get("uniqueID") or task_id
        
        task['_computed_id'] = int(task_id) if task_id else (i + 1)
        task['_computed_uid'] = int(uid) if uid else task['_computed_id']
        
        by_name[name] = task
        by_id[task['_computed_id']] = task
        by_uid[task['_computed_uid']] = task
    
    return by_name, by_id, by_uid


def apply_corrections(tasks, corrections):
    """Применяет корректировки к задачам."""
    if not corrections:
        return tasks
    
    task_cors = corrections.get("task_corrections", {})
    
    for task in tasks:
        name = task.get("name", "")
        task_id = task.get('_computed_id')
        
        corr = None
        if name in task_cors:
            corr = task_cors[name]
        elif str(task_id) in task_cors:
            corr = task_cors[str(task_id)]
        
        if corr:
            if "duration" in corr:
                task["duration"] = f"{corr['duration']}.0d"
            
            if "start" in corr:
                task["start"] = corr["start"]
            
            if "note" in corr:
                existing = task.get("notes") or ""
                prefix = "⚠️ " if not existing.startswith("⚠️") else ""
                task["notes"] = prefix + corr["note"] + ("\n" + existing if existing else "")
    
    return tasks


def generate_mspdi(data, corrections=None):
    """Генерирует MSPDI XML документ."""
    
    tasks = data.get("tasks", [])
    resources = data.get("resources", [])
    assignments = data.get("assignments", [])
    project_info = data.get("summary", {})
    
    # Применяем корректировки
    tasks = apply_corrections(tasks, corrections)
    
    # Строим индексы
    by_name, by_id, by_uid = build_task_index(tasks)
    
    # Корневой элемент
    root = Element("Project")
    root.set("xmlns", "http://schemas.microsoft.com/project")
    
    # Свойства проекта
    SubElement(root, "Name").text = project_info.get("file_name", "Project") or "Project"
    SubElement(root, "Subject").text = project_info.get("file_name", "")
    
    # Даты проекта
    start_dates = [parse_date(t.get("start")) for t in tasks if t.get("start")]
    finish_dates = [parse_date(t.get("finish")) for t in tasks if t.get("finish")]
    
    if start_dates:
        SubElement(root, "StartDate").text = format_date(min(start_dates))
    if finish_dates:
        SubElement(root, "FinishDate").text = format_date(max(finish_dates))
    
    # Заметки проекта
    project_notes = project_info.get("notes", "")
    if corrections and "project_notes" in corrections:
        project_notes += "\n\n" + corrections["project_notes"]
    if project_notes:
        SubElement(root, "Notes").text = project_notes.strip()
    
    # Календари
    calendars = SubElement(root, "Calendars")
    cal = SubElement(calendars, "Calendar")
    SubElement(cal, "UID").text = "1"
    SubElement(cal, "Name").text = "Standard"
    SubElement(cal, "IsBaseCalendar").text = "1"
    SubElement(cal, "IsBaselineCalendar").text = "0"
    SubElement(cal, "BaseCalendarUID").text = "-1"
    
    week_days = SubElement(cal, "WeekDays")
    for day in range(1, 8):
        wd = SubElement(week_days, "WeekDay")
        SubElement(wd, "DayType").text = str(day)
        if day in [2, 3, 4, 5, 6]:
            SubElement(wd, "DayWorking").text = "1"
            wt = SubElement(wd, "WorkingTimes")
            wtp = SubElement(wt, "WorkingTime")
            SubElement(wtp, "FromTime").text = "08:00:00"
            SubElement(wtp, "ToTime").text = "17:00:00"
        else:
            SubElement(wd, "DayWorking").text = "0"
    
    # Ресурсы
    resources_elem = SubElement(root, "Resources")
    
    # Пустой ресурс (ID=0)
    null_res = SubElement(resources_elem, "Resource")
    SubElement(null_res, "UID").text = "0"
    SubElement(null_res, "ID").text = "0"
    SubElement(null_res, "Type").text = "0"
    SubElement(null_res, "IsNull").text = "1"
    SubElement(null_res, "MaxUnits").text = "1"
    
    # Реальные ресурсы
    for res in resources:
        res_elem = SubElement(resources_elem, "Resource")
        uid = res.get("unique_id") or res.get("id") or 0
        res_id = res.get("id") or uid
        
        SubElement(res_elem, "UID").text = str(uid)
        SubElement(res_elem, "ID").text = str(res_id)
        
        name = res.get("name", "")
        SubElement(res_elem, "Name").text = name
        
        # Тип ресурса (0=Work, 1=Material, 2=Cost)
        rtype = res.get("type", "WORK")
        if isinstance(rtype, str):
            if "MATERIAL" in rtype.upper():
                SubElement(res_elem, "Type").text = "1"
            elif "COST" in rtype.upper():
                SubElement(res_elem, "Type").text = "2"
            else:
                SubElement(res_elem, "Type").text = "0"
        else:
            SubElement(res_elem, "Type").text = "0"
        
        SubElement(res_elem, "IsNull").text = "0"
        
        if res.get("max_units"):
            SubElement(res_elem, "MaxUnits").text = str(res.get("max_units"))
        
        if res.get("email"):
            SubElement(res_elem, "EmailAddress").text = str(res.get("email"))
        
        if res.get("group"):
            SubElement(res_elem, "Group").text = str(res.get("group"))
        
        if res.get("notes"):
            SubElement(res_elem, "Notes").text = str(res.get("notes"))
    
    # Задачи
    tasks_elem = SubElement(root, "Tasks")
    
    for task in tasks:
        task_elem = SubElement(tasks_elem, "Task")
        
        uid = task.get('_computed_uid', 0)
        task_id = task.get('_computed_id', 0)
        
        SubElement(task_elem, "UID").text = str(uid)
        SubElement(task_elem, "ID").text = str(task_id)
        
        name = task.get("name", "")
        SubElement(task_elem, "Name").text = name
        
        # Тип задачи
        SubElement(task_elem, "Type").text = "1"
        
        # Процент выполнения
        pct = task.get("percent_complete", 0)
        if pct is None:
            pct = 0
        try:
            pct = float(pct)
        except (ValueError, TypeError):
            pct = 0
        SubElement(task_elem, "PercentComplete").text = str(int(pct))
        
        # Длительность
        dur_str = task.get("duration", "1.0d")
        dur_days = parse_duration(dur_str)
        SubElement(task_elem, "Duration").text = format_duration(dur_days)
        SubElement(task_elem, "DurationFormat").text = "7"
        
        # Даты
        start = parse_date(task.get("start"))
        finish = parse_date(task.get("finish"))
        
        if corrections:
            task_cors = corrections.get("task_corrections", {})
            corr = task_cors.get(name) or task_cors.get(str(task_id))
            if corr and "duration" in corr and start:
                finish = add_workdays(start, corr["duration"])
        
        if start:
            SubElement(task_elem, "Start").text = format_date(start)
        if finish:
            SubElement(task_elem, "Finish").text = format_date(finish)
        
        # Веха
        is_milestone = task.get("milestone", False)
        SubElement(task_elem, "Milestone").text = "1" if is_milestone else "0"
        
        # Критическая
        is_critical = task.get("critical", False)
        SubElement(task_elem, "Critical").text = "1" if is_critical else "0"
        
        # Сводная задача (summary)
        is_summary = task.get("summary", False)
        SubElement(task_elem, "Summary").text = "1" if is_summary else "0"
        
        # WBS
        wbs = task.get("wbs", "")
        if wbs:
            SubElement(task_elem, "WBS").text = wbs
        
        # Outline Level и Number
        outline_level = task.get("outline_level", 0)
        if outline_level:
            SubElement(task_elem, "OutlineLevel").text = str(outline_level)
        
        outline_number = task.get("outline_number", "")
        if outline_number:
            SubElement(task_elem, "OutlineNumber").text = str(outline_number)
        
        # Приоритет
        priority = task.get("priority", 500)
        if isinstance(priority, str):
            priority_match = re.search(r'(\d+)', priority)
            priority = int(priority_match.group(1)) if priority_match else 500
        SubElement(task_elem, "Priority").text = str(priority)
        
        # Заметки
        notes = task.get("notes", "")
        if notes:
            SubElement(task_elem, "Notes").text = notes
        
        # Флаги
        SubElement(task_elem, "HideBar").text = "0"
        SubElement(task_elem, "Rollup").text = "0"
        SubElement(task_elem, "FixedCostAccrual").text = "3"
        SubElement(task_elem, "ConstraintType").text = "0"
        SubElement(task_elem, "CalendarUID").text = "-1"
    
    # Зависимости (PredecessorLinks)
    for task in tasks:
        preds_str = task.get("predecessors", "")
        if preds_str and isinstance(preds_str, str):
            # Парсим формат: "[Task id=X uniqueID=Y name=...] -> [Task id=Z ...]"
            # Ищем все предшественников перед "->"
            pred_matches = re.findall(r'\[Task id=(\d+)[^\]]*\]\s*->', preds_str)
            
            if pred_matches:
                task_id = task.get('_computed_id')
                for task_elem in tasks_elem.findall("Task"):
                    if task_elem.find("ID").text == str(task_id):
                        for pred_id_str in pred_matches:
                            try:
                                pred_id = int(pred_id_str)
                                pred_task = by_id.get(pred_id)
                                if pred_task:
                                    pred_uid = pred_task.get('_computed_uid', pred_id)
                                    link = SubElement(task_elem, "PredecessorLink")
                                    SubElement(link, "PredecessorUID").text = str(pred_uid)
                                    SubElement(link, "Type").text = "1"
                                    SubElement(link, "CrossProject").text = "0"
                                    SubElement(link, "LinkLag").text = "0"
                                    SubElement(link, "LagFormat").text = "7"
                            except ValueError:
                                pass
                        break
    
    # Назначения (Assignments)
    assignments_elem = SubElement(root, "Assignments")
    
    for assign in assignments:
        assign_elem = SubElement(assignments_elem, "Assignment")
        
        # UID назначения
        task_uid = assign.get("task_unique_id") or assign.get("task_id", 0)
        res_uid = assign.get("resource_unique_id") or assign.get("resource_id", 0)
        
        assign_uid = f"{task_uid}_{res_uid}" if task_uid and res_uid else "0"
        SubElement(assign_elem, "UID").text = assign_uid
        SubElement(assign_elem, "TaskUID").text = str(task_uid)
        SubElement(assign_elem, "ResourceUID").text = str(res_uid)
        
        if assign.get("units"):
            SubElement(assign_elem, "Units").text = str(assign.get("units"))
        
        if assign.get("work"):
            SubElement(assign_elem, "Work").text = str(assign.get("work"))
        
        if assign.get("actual_work"):
            SubElement(assign_elem, "ActualWork").text = str(assign.get("actual_work"))
        
        if assign.get("remaining_work"):
            SubElement(assign_elem, "RemainingWork").text = str(assign.get("remaining_work"))
        
        if assign.get("cost"):
            SubElement(assign_elem, "Cost").text = str(assign.get("cost"))
        
        if assign.get("actual_cost"):
            SubElement(assign_elem, "ActualCost").text = str(assign.get("actual_cost"))
    
    return prettify_xml(root)


def main():
    parser = argparse.ArgumentParser(description="MPP Writer — генерация MS Project XML")
    parser.add_argument("input", help="Входной JSON файл (из mpp_read.py)")
    parser.add_argument("--output", "-o", default="output.xml", help="Выходной XML файл")
    parser.add_argument("--corrections", "-c", help="JSON файл с корректировками")
    
    args = parser.parse_args()
    
    with open(args.input, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    corrections = None
    if args.corrections:
        with open(args.corrections, 'r', encoding='utf-8') as f:
            corrections = json.load(f)
    
    xml_content = generate_mspdi(data, corrections)
    
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(xml_content)
    
    print(f"✓ Сгенерирован: {args.output}")
    print(f"  Задач: {len(data.get('tasks', []))}")
    print(f"  Ресурсов: {len(data.get('resources', []))}")
    print(f"  Назначений: {len(data.get('assignments', []))}")
    
    if corrections:
        cor_count = len(corrections.get("task_corrections", {}))
        print(f"  Применено корректировок: {cor_count}")


if __name__ == "__main__":
    main()

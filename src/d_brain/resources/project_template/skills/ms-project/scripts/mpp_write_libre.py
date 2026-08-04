#!/usr/bin/env python3
"""
MPP Writer for ProjectLibre — генерация совместимого MS Project XML.

ProjectLibre требует полную структуру MSPDI со всеми обязательными полями.
"""

import argparse
import json
import re
import sys
from datetime import datetime, timedelta
from xml.etree.ElementTree import Element, SubElement, tostring
from xml.dom import minidom


def prettify_xml(elem):
    rough_string = tostring(elem, encoding='unicode')
    reparsed = minidom.parseString(rough_string)
    return reparsed.toprettyxml(indent="    ")


def parse_duration(dur_str):
    if not dur_str:
        return 1
    match = re.match(r'(\d+(?:\.\d+)?)\s*([dwhm]?)', dur_str.lower())
    if match:
        val = float(match.group(1))
        unit = match.group(2)
        if unit == 'h': return max(1, int(val / 8))
        elif unit == 'w': return int(val * 5)
        elif unit == 'm': return int(val * 20)
        else: return int(val)
    return 1


def format_duration(days):
    return f"PT{days * 8}H0M0S"


def parse_date(date_str):
    if not date_str: return None
    for fmt in ["%Y-%m-%dT%H:%M:%S", "%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"]:
        try: return datetime.strptime(date_str[:19] if 'T' in fmt or ' ' in fmt else date_str[:10], fmt)
        except ValueError: continue
    return None


def format_date_xml(dt):
    if dt is None: return ""
    return dt.strftime("%Y-%m-%dT%H:%M:%S")


def add_workdays(start, days):
    if days <= 0: return start
    current, added = start, 0
    while added < days:
        current += timedelta(days=1)
        if current.weekday() < 5: added += 1
    return current


def build_task_index(tasks):
    by_id, by_uid = {}, {}
    for i, task in enumerate(tasks):
        task_id = task.get("id") or task.get("ID") or (i + 1)
        uid = task.get("unique_id") or task.get("uniqueID") or task_id
        task['_computed_id'] = int(task_id) if task_id else (i + 1)
        task['_computed_uid'] = int(uid) if uid else task['_computed_id']
        by_id[task['_computed_id']] = task
        by_uid[task['_computed_uid']] = task
    return by_id, by_uid


def apply_corrections(tasks, corrections):
    if not corrections: return tasks
    task_cors = corrections.get("task_corrections", {})
    for task in tasks:
        name = task.get("name", "")
        task_id = task.get('_computed_id')
        corr = task_cors.get(name) or task_cors.get(str(task_id))
        if corr:
            if "duration" in corr: task["duration"] = f"{corr['duration']}.0d"
            if "start" in corr: task["start"] = corr["start"]
            if "note" in corr:
                existing = task.get("notes") or ""
                task["notes"] = "[CORRECTED] " + corr["note"] + ("\n" + existing if existing else "")
    return tasks


def generate_libre_xml(data, corrections=None):
    tasks = data.get("tasks", [])
    resources = data.get("resources", [])
    assignments = data.get("assignments", [])
    project_info = data.get("summary", {})
    
    tasks = apply_corrections(tasks, corrections)
    by_id, by_uid = build_task_index(tasks)
    
    root = Element("Project")
    root.set("xmlns", "http://schemas.microsoft.com/project")
    
    # === СВОЙСТВА ПРОЕКТА (обязательные для ProjectLibre) ===
    SubElement(root, "SaveVersion").text = "14"
    SubElement(root, "Name").text = project_info.get("file_name", "Project") or "Project"
    SubElement(root, "Title").text = ""
    SubElement(root, "Manager").text = ""
    SubElement(root, "ScheduleFromStart").text = "1"
    
    start_dates = [parse_date(t.get("start")) for t in tasks if t.get("start")]
    finish_dates = [parse_date(t.get("finish")) for t in tasks if t.get("finish")]
    
    project_start = min(start_dates) if start_dates else datetime(2026, 4, 1, 9, 0, 0)
    project_finish = max(finish_dates) if finish_dates else datetime(2026, 7, 30, 18, 0, 0)
    
    SubElement(root, "StartDate").text = format_date_xml(project_start)
    SubElement(root, "FinishDate").text = format_date_xml(project_finish)
    SubElement(root, "FYStartDate").text = "1"
    SubElement(root, "CriticalSlackLimit").text = "0"
    SubElement(root, "CurrencyDigits").text = "2"
    SubElement(root, "CurrencySymbol").text = "$"
    SubElement(root, "CurrencySymbolPosition").text = "0"
    SubElement(root, "CalendarUID").text = "1"
    SubElement(root, "DefaultStartTime").text = "08:00:00"
    SubElement(root, "DefaultFinishTime").text = "17:00:00"
    SubElement(root, "MinutesPerDay").text = "480"
    SubElement(root, "MinutesPerWeek").text = "2400"
    SubElement(root, "DaysPerMonth").text = "20"
    SubElement(root, "DefaultTaskType").text = "0"
    SubElement(root, "DefaultFixedCostAccrual").text = "2"
    SubElement(root, "DefaultStandardRate").text = "10"
    SubElement(root, "DefaultOvertimeRate").text = "15"
    SubElement(root, "DurationFormat").text = "7"
    SubElement(root, "WorkFormat").text = "2"
    SubElement(root, "EditableActualCosts").text = "0"
    SubElement(root, "HonorConstraints").text = "0"
    SubElement(root, "EarnedValueMethod").text = "0"
    SubElement(root, "InsertedProjectsLikeSummary").text = "0"
    SubElement(root, "MultipleCriticalPaths").text = "0"
    SubElement(root, "NewTasksEffortDriven").text = "0"
    SubElement(root, "NewTasksEstimated").text = "1"
    SubElement(root, "SplitsInProgressTasks").text = "0"
    SubElement(root, "SpreadActualCost").text = "0"
    SubElement(root, "SpreadPercentComplete").text = "0"
    SubElement(root, "TaskUpdatesResource").text = "1"
    SubElement(root, "FiscalYearStart").text = "0"
    SubElement(root, "WeekStartDay").text = "1"
    SubElement(root, "MoveCompletedEndsBack").text = "0"
    SubElement(root, "MoveRemainingStartsBack").text = "0"
    SubElement(root, "MoveRemainingStartsForward").text = "0"
    SubElement(root, "MoveCompletedEndsForward").text = "0"
    SubElement(root, "BaselineForEarnedValue").text = "0"
    SubElement(root, "AutoAddNewResourcesAndTasks").text = "1"
    SubElement(root, "CurrentDate").text = format_date_xml(project_start)
    SubElement(root, "MicrosoftProjectServerURL").text = "1"
    SubElement(root, "Autolink").text = "1"
    SubElement(root, "NewTaskStartDate").text = "0"
    SubElement(root, "DefaultTaskEVMethod").text = "0"
    SubElement(root, "ProjectExternallyEdited").text = "0"
    SubElement(root, "ActualsInSync").text = "0"
    SubElement(root, "RemoveFileProperties").text = "0"
    SubElement(root, "AdminProject").text = "0"
    
    # === КАЛЕНДАРИ ===
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
        if day in [2, 3, 4, 5, 6]:  # Пн-Пт
            SubElement(wd, "DayWorking").text = "1"
            wt = SubElement(wd, "WorkingTimes")
            wtp1 = SubElement(wt, "WorkingTime")
            SubElement(wtp1, "FromTime").text = "08:00:00"
            SubElement(wtp1, "ToTime").text = "12:00:00"
            wtp2 = SubElement(wt, "WorkingTime")
            SubElement(wtp2, "FromTime").text = "13:00:00"
            SubElement(wtp2, "ToTime").text = "17:00:00"
        else:  # Сб, Вс
            SubElement(wd, "DayWorking").text = "0"
    
    # === ЗАДАЧИ ===
    tasks_elem = SubElement(root, "Tasks")
    
    for task in tasks:
        task_elem = SubElement(tasks_elem, "Task")
        
        uid = task.get('_computed_uid', 0)
        task_id = task.get('_computed_id', 0)
        
        SubElement(task_elem, "UID").text = str(uid)
        SubElement(task_elem, "ID").text = str(task_id)
        SubElement(task_elem, "Name").text = task.get("name", "")
        SubElement(task_elem, "Type").text = "0" if not task.get("summary") else "1"
        SubElement(task_elem, "IsNull").text = "0"
        SubElement(task_elem, "CreateDate").text = format_date_xml(project_start)
        
        wbs = task.get("wbs") or task.get("WBS") or str(task_id)
        SubElement(task_elem, "WBS").text = str(wbs)
        
        outline_number = task.get("outline_number") or task.get("OutlineNumber") or str(task_id)
        SubElement(task_elem, "OutlineNumber").text = str(outline_number)
        
        outline_level = task.get("outline_level") or task.get("OutlineLevel") or 0
        SubElement(task_elem, "OutlineLevel").text = str(outline_level)
        
        priority = task.get("priority", 500)
        if isinstance(priority, str):
            m = re.search(r'(\d+)', priority)
            priority = int(m.group(1)) if m else 500
        SubElement(task_elem, "Priority").text = str(priority)
        
        dur_days = parse_duration(task.get("duration", "1.0d"))
        
        start = parse_date(task.get("start"))
        finish = parse_date(task.get("finish"))
        
        if corrections:
            task_cors = corrections.get("task_corrections", {})
            corr = task_cors.get(task.get("name", "")) or task_cors.get(str(task_id))
            if corr and "duration" in corr and start:
                dur_days = corr["duration"]
                finish = add_workdays(start, dur_days)
        
        if start: SubElement(task_elem, "Start").text = format_date_xml(start)
        if finish: SubElement(task_elem, "Finish").text = format_date_xml(finish)
        
        SubElement(task_elem, "Duration").text = format_duration(dur_days)
        SubElement(task_elem, "DurationFormat").text = "7"
        
        # Для всех задач добавляем Resume
        # Для 0%: Resume = Finish, Stop = эпоха (чтобы показать "не начиналась")
        # Для 100%: Resume = Finish, Stop = Finish-1h
        # Для 1-99%: Resume = Start, Stop = Start (или пересчитанная дата)
        pct = task.get("percent_complete", 0) or 0
        try: pct_val = float(pct)
        except: pct_val = 0
        
        if pct_val == 0 and finish:
            SubElement(task_elem, "Resume").text = format_date_xml(finish)
            SubElement(task_elem, "Stop").text = "1970-01-01T00:00:00"
        elif pct_val >= 100 and finish:
            SubElement(task_elem, "Resume").text = format_date_xml(finish)
            SubElement(task_elem, "Stop").text = format_date_xml(finish.replace(hour=finish.hour-1 if finish.hour > 0 else 0))
        elif start:
            SubElement(task_elem, "Resume").text = format_date_xml(start)
            # Для частично выполненных — Stop между Start и Finish
            if finish and pct_val > 0:
                # Примерная дата остановки
                total_hours = (finish - start).total_seconds() / 3600
                completed_hours = total_hours * pct_val / 100
                stop_date = start + timedelta(hours=completed_hours)
                SubElement(task_elem, "Stop").text = format_date_xml(stop_date)
            else:
                SubElement(task_elem, "Stop").text = format_date_xml(start)
        SubElement(task_elem, "ResumeValid").text = "0"
        SubElement(task_elem, "EffortDriven").text = "1"
        SubElement(task_elem, "Recurring").text = "0"
        SubElement(task_elem, "OverAllocated").text = "0"
        SubElement(task_elem, "Estimated").text = "0"
        SubElement(task_elem, "Milestone").text = "1" if task.get("milestone") else "0"
        SubElement(task_elem, "Summary").text = "1" if task.get("summary") else "0"
        SubElement(task_elem, "Critical").text = "1" if task.get("critical") else "0"
        SubElement(task_elem, "IsSubproject").text = "0"
        SubElement(task_elem, "IsSubprojectReadOnly").text = "0"
        SubElement(task_elem, "ExternalTask").text = "0"
        SubElement(task_elem, "FixedCostAccrual").text = "3"
        
        SubElement(task_elem, "PercentComplete").text = str(int(pct_val))
        SubElement(task_elem, "PercentWorkComplete").text = str(int(pct_val))
        SubElement(task_elem, "RemainingDuration").text = format_duration(dur_days)
        # Для 0%: ConstraintType=4 но ConstraintDate=Finish (задача не может начаться раньше Finish = никогда)
        # Для остальных: ConstraintType=4 + ConstraintDate=Start
        if pct_val == 0:
            SubElement(task_elem, "ConstraintType").text = "4"
            SubElement(task_elem, "ConstraintDate").text = format_date_xml(finish) if finish else format_date_xml(start)
        else:
            SubElement(task_elem, "ConstraintType").text = "4"
            SubElement(task_elem, "ConstraintDate").text = format_date_xml(start) if start else "1970-01-01T00:00:00"
        SubElement(task_elem, "LevelAssignments").text = "0"
        SubElement(task_elem, "LevelingCanSplit").text = "0"
        SubElement(task_elem, "LevelingDelay").text = "0"
        SubElement(task_elem, "LevelingDelayFormat").text = "8"
        SubElement(task_elem, "IgnoreResourceCalendar").text = "0"
        SubElement(task_elem, "HideBar").text = "0"
        SubElement(task_elem, "Rollup").text = "0"
        SubElement(task_elem, "EarnedValueMethod").text = "0"
        SubElement(task_elem, "Active").text = "1"
        SubElement(task_elem, "Manual").text = "0"
        
        notes = task.get("notes", "")
        if notes: SubElement(task_elem, "Notes").text = notes
    
    # Зависимости
    for task in tasks:
        preds_str = task.get("predecessors", "")
        if preds_str and isinstance(preds_str, str):
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
                            except ValueError: pass
                        break
    
    # === РЕСУРСЫ ===
    resources_elem = SubElement(root, "Resources")
    
    for idx, res in enumerate(resources):
        res_elem = SubElement(resources_elem, "Resource")
        uid = res.get("unique_id") or res.get("id") or (idx + 1)
        res_id = res.get("id") or uid
        
        SubElement(res_elem, "UID").text = str(uid)
        SubElement(res_elem, "ID").text = str(res_id)
        SubElement(res_elem, "Name").text = res.get("name", "")
        
        rtype = res.get("type", "WORK")
        if isinstance(rtype, str):
            if "MATERIAL" in rtype.upper(): SubElement(res_elem, "Type").text = "1"
            elif "COST" in rtype.upper(): SubElement(res_elem, "Type").text = "2"
            else: SubElement(res_elem, "Type").text = "1"
        else:
            SubElement(res_elem, "Type").text = "1"
        
        SubElement(res_elem, "IsNull").text = "0"
        SubElement(res_elem, "Initials").text = res.get("name", "")[0] if res.get("name") else "R"
        SubElement(res_elem, "Group").text = ""
        SubElement(res_elem, "EmailAddress").text = ""
        SubElement(res_elem, "MaxUnits").text = str(res.get("max_units", 1.0) or 1.0)
        SubElement(res_elem, "PeakUnits").text = str(res.get("max_units", 1.0) or 1.0)
        SubElement(res_elem, "OverAllocated").text = "0"
        SubElement(res_elem, "CanLevel").text = "0"
        SubElement(res_elem, "AccrueAt").text = "3"
        SubElement(res_elem, "StandardRateFormat").text = "3"
        SubElement(res_elem, "OvertimeRateFormat").text = "3"
        SubElement(res_elem, "CalendarUID").text = "-1"
        SubElement(res_elem, "IsGeneric").text = "0"
        SubElement(res_elem, "IsInactive").text = "0"
        SubElement(res_elem, "IsEnterprise").text = "0"
        SubElement(res_elem, "IsBudget").text = "0"
        SubElement(res_elem, "AvailabilityPeriods")
    
    # === НАЗНАЧЕНИЯ ===
    assignments_elem = SubElement(root, "Assignments")
    
    for idx, assign in enumerate(assignments):
        assign_elem = SubElement(assignments_elem, "Assignment")
        
        task_uid = assign.get("task_unique_id") or assign.get("task_id", 0)
        res_uid = assign.get("resource_unique_id") or assign.get("resource_id", 0)
        
        # Найти задачу и ресурс для дат
        task = by_uid.get(task_uid)
        start = parse_date(task.get("start")) if task else None
        finish = parse_date(task.get("finish")) if task else None
        dur_days = parse_duration(task.get("duration", "1.0d")) if task else 1
        
        SubElement(assign_elem, "UID").text = str(idx + 1)
        SubElement(assign_elem, "TaskUID").text = str(task_uid)
        SubElement(assign_elem, "ResourceUID").text = str(res_uid)
        
        if finish: SubElement(assign_elem, "Finish").text = format_date_xml(finish)
        SubElement(assign_elem, "HasFixedRateUnits").text = "1"
        SubElement(assign_elem, "FixedMaterial").text = "0"
        SubElement(assign_elem, "RemainingWork").text = format_duration(dur_days)
        if start: SubElement(assign_elem, "Start").text = format_date_xml(start)
        
        # Resume для назначения
        if start: SubElement(assign_elem, "Resume").text = format_date_xml(start)
        
        units = assign.get("units", 100)
        SubElement(assign_elem, "Units").text = str(units / 100.0 if units else 1.0)
        SubElement(assign_elem, "Work").text = format_duration(dur_days)
        SubElement(assign_elem, "WorkContour").text = "0"
        
        # TimephasedData
        if start and finish:
            tpd = SubElement(assign_elem, "TimephasedData")
            SubElement(tpd, "Type").text = "2"
            SubElement(tpd, "UID").text = str(idx + 1)
            SubElement(tpd, "Start").text = format_date_xml(start)
            SubElement(tpd, "Finish").text = format_date_xml(finish)
            SubElement(tpd, "Unit").text = "3"
            SubElement(tpd, "Value").text = format_duration(dur_days)
    
    xml_str = prettify_xml(root)
    # Добавляем standalone="yes" в XML declaration
    xml_str = xml_str.replace('<?xml version="1.0" ?>', '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>')
    return xml_str


def main():
    parser = argparse.ArgumentParser(description="MPP Writer for ProjectLibre")
    parser.add_argument("input", help="Входной JSON файл")
    parser.add_argument("--output", "-o", default="output.xml", help="Выходной XML файл")
    parser.add_argument("--corrections", "-c", help="JSON файл с корректировками")
    
    args = parser.parse_args()
    
    with open(args.input, 'r', encoding='utf-8') as f:
        data = json.load(f)
    
    corrections = None
    if args.corrections:
        with open(args.corrections, 'r', encoding='utf-8') as f:
            corrections = json.load(f)
    
    xml_content = generate_libre_xml(data, corrections)
    
    with open(args.output, 'w', encoding='utf-8') as f:
        f.write(xml_content)
    
    print(f"✓ Сгенерирован (ProjectLibre): {args.output}")
    print(f"  Задач: {len(data.get('tasks', []))}")
    print(f"  Ресурсов: {len(data.get('resources', []))}")
    print(f"  Назначений: {len(data.get('assignments', []))}")


if __name__ == "__main__":
    main()

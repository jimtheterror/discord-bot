import csv
from dataclasses import dataclass
from typing import List, Dict, Optional
import random
from collections import defaultdict
import os

ROBOT_IDS = ['Joystick 51', 'Joystick 52', 'Joystick 53', 'Joystick 54', 'Joystick 55', 'Joystick 56', 'Joystick 57', 'Joystick 58']

@dataclass
class TimeSlot:
    time: str
    activity: str
    is_comm_lead: bool = False
    robot_id: Optional[str] = None

class GroupScheduler:
    def __init__(self, contractor_names: List[str]):
        if len(contractor_names) < 5:
            raise ValueError("Need at least 5 contractors")
        
        self.contractor_names = contractor_names
        self.group_template = self._load_group_template()
        self.contractor_groups = self._assign_to_groups()
        self.robot_assignments = {}  # Contractor -> Robot ID mapping
        
    def _assign_robots(self):
        """Assign robots to contractors who will be doing CC or RP"""
        available_robots = ROBOT_IDS.copy()
        random.shuffle(available_robots)
        
        # Reset robot assignments
        self.robot_assignments = {}
        
        # Find all contractors who will be doing CC or RP at any point
        pilot_contractors = set()
        for group_name, contractors in self.contractor_groups.items():
            group_schedule = self.group_template[group_name]
            for slot in group_schedule:
                if slot['activity'] in ['CC', 'RP']:
                    pilot_contractors.update(contractors)
        
        # Assign robots to these contractors
        for contractor in pilot_contractors:
            if available_robots:
                self.robot_assignments[contractor] = available_robots.pop()
            else:
                # If we run out of robots, reuse existing ones
                self.robot_assignments[contractor] = random.choice(ROBOT_IDS)

    def _load_group_template(self) -> Dict[str, List[Dict[str, str]]]:
        """Load the group schedule template from CSV"""
        template = defaultdict(list)
        
        # Get the directory containing the current file
        current_dir = os.path.dirname(os.path.abspath(__file__))
        groups_schedule_path = os.path.join(current_dir, 'groups_schedule.csv')
        
        with open(groups_schedule_path, 'r') as f:
            reader = csv.DictReader(f)
            for row in reader:
                time = row['Event Start Time']
                template['Group1'].append({'time': time, 'activity': row['Group1']})
                template['Group2'].append({'time': time, 'activity': row['Group2']})
                template['Group3'].append({'time': time, 'activity': row['Group3']})
        
        return template
    
    def _assign_to_groups(self) -> Dict[str, List[str]]:
        """Distribute contractors evenly among groups"""
        groups = {'Group1': [], 'Group2': [], 'Group3': []}
        contractors = self.contractor_names.copy()
        random.shuffle(contractors)  # Randomize assignment
        
        # Calculate minimum contractors per group
        min_per_group = len(contractors) // 3
        extra = len(contractors) % 3
        
        # Distribute contractors
        current_group = 1
        for contractor in contractors:
            group_name = f'Group{current_group}'
            groups[group_name].append(contractor)
            
            # Move to next group if current group is full
            if len(groups[group_name]) >= min_per_group + (1 if extra > 0 else 0):
                current_group += 1
                extra = max(0, extra - 1)
        
        return groups
    
    def _assign_comm_leads(self, time_slot: str, schedules: Dict[str, List[TimeSlot]]):
        """Assign a random comm lead from contractors doing DL"""
        # Find all contractors doing DL in this time slot
        dl_contractors = []
        for contractor, schedule in schedules.items():
            for slot in schedule:
                if slot.time == time_slot and slot.activity == 'DL':
                    dl_contractors.append(contractor)
                    break
        
        if dl_contractors:
            # Randomly select one as comm lead
            comm_lead = random.choice(dl_contractors)
            # Update their schedule
            for slot in schedules[comm_lead]:
                if slot.time == time_slot:
                    slot.is_comm_lead = True
                    break
    
    def generate_schedule(self) -> Dict[str, List[TimeSlot]]:
        """Generate individual schedules for all contractors"""
        # First, assign robots to contractors who will be piloting
        self._assign_robots()
        
        schedules = {}
        
        # Create initial schedules based on group assignments
        for group_name, contractors in self.contractor_groups.items():
            group_schedule = self.group_template[group_name]
            
            for contractor in contractors:
                schedule = []
                for slot in group_schedule:
                    time_slot = TimeSlot(
                        time=slot['time'],
                        activity=slot['activity']
                    )
                    
                    # If contractor is doing CC or RP and has a robot assigned
                    if slot['activity'] in ['CC', 'RP'] and contractor in self.robot_assignments:
                        time_slot.robot_id = self.robot_assignments[contractor]
                    
                    schedule.append(time_slot)
                
                schedules[contractor] = schedule
        
        # Assign comm leads for each time slot where DL occurs
        for time_slot in self.group_template['Group1']:  # Use Group1 as reference for time slots
            if time_slot['time'] != '9:00':  # Skip end of shift
                self._assign_comm_leads(time_slot['time'], schedules)
        
        return schedules
    
    def export_to_csv(self, filename: str):
        """Export the schedule to CSV"""
        schedules = self.generate_schedule()
        
        # Prepare the rows
        rows = [['Time', 'Contractor', 'Activity', 'Comm Lead', 'Robot ID']]
        
        # Get all time slots from the template
        time_slots = [slot['time'] for slot in self.group_template['Group1']]
        
        # Add a row for each contractor at each time
        for time in time_slots:
            for contractor in sorted(self.contractor_names):
                # Find the corresponding time slot in contractor's schedule
                slot = next(slot for slot in schedules[contractor] if slot.time == time)
                rows.append([
                    time,
                    contractor,
                    slot.activity,
                    'Yes' if slot.is_comm_lead else 'No',
                    slot.robot_id or ''
                ])
        
        # Write to CSV
        with open(filename, 'w', newline='') as f:
            writer = csv.writer(f)
            writer.writerows(rows)
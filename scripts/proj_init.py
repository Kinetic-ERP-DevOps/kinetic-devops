import os
import subprocess
import sys
import datetime
import json
import re
import fnmatch
import stat
from typing import Dict, Any, Union, List, Tuple, Set

# --- Constants ---
MAX_PRIORITY_INT = 999999

# Minimal arbitration rules for essential fallbacks and mandatory path structure
MINIMAL_ATTRIBUTE_ARBITRATION = [
    # Mandatory Path Structure Extraction/Validation
    {
        "Priority": -10,
        "SourceAttribute": "DeriveValue",
        "RawInputType": "CommandLineArgument",
        "InputKey": "path_parts_0",
        "SubstitutionName": "path_root",
        "SubstitutionType": "String",
        "Required": True,
        "Description": "Mandatory extraction of path root (Type) for template matching."
    },
    {
        "Priority": -9,
        "SourceAttribute": "DeriveValue",
        "RawInputType": "CommandLineArgument",
        "InputKey": "path_parts_1",
        "SubstitutionName": "project_name",
        "SubstitutionType": "String",
        "Required": True,
        "Description": "Mandatory extraction of project name."
    },
    # Final Fallback for core values
    {
        "Priority": MAX_PRIORITY_INT,
        "SourceAttribute": "DeriveValue",
        "TimeType": "UTCTime",
        "SubstitutionName": "export_date",
        "SubstitutionType": "Date",
        "Description": "Current UTC date (minimal fallback)",
        "AutoAccept": True
    },
    {
        "Priority": MAX_PRIORITY_INT,
        "SourceAttribute": "DeriveValue",
        "FixedValue": "Initialization commit (MAX PRIORITY FALLBACK)",
        "SubstitutionName": "export_commit_message",
        "SubstitutionType": "String",
        "Description": "Default commit message fallback.",
        "AutoAccept": True
    },
    # Final Fallback for file actions (Skip everything else) - Must have highest priority number
    {
        "Priority": MAX_PRIORITY_INT,
        "TargetAttribute": ["*"],
        "Pattern": ["*"],
        "Description": "Default: Skip all files if no other rule matches.",
        "Commit": "Never",
        "Actions": [
            {"Action": "Skip", "Description": "Default Skip"}
        ]
    }
]

# --- Global State Store (Used to pass files between ProgramFlow steps) ---
GLOBAL_STATE = {
    "files_to_create": {},      # Dict[str, str]: file_path -> content
    "files_deleted": set(),     # Set[str]: file_path
    "max_warning_exit_code": 0  # int
}

# --- AttributeArbitrator Class (Unified Logic) ---

class AttributeArbitrator:
    
    # --- Core Utility Methods ---

    @staticmethod
    def _check_file_attribute(attribute: str, target_path: str, export_date: datetime.date) -> bool:
        """Checks a specific file/path attribute (used for file conflict rules)."""
        attribute = attribute.lower()
        
        if attribute == "writable": return os.access(target_path, os.W_OK)
        if attribute == "readonly": return os.path.exists(target_path) and not os.access(target_path, os.W_OK)
        if attribute == "directory": return os.path.isdir(target_path)
        if attribute == "*": return True
        
        if os.path.exists(target_path) and os.path.isfile(target_path):
            try:
                file_mtime = datetime.date.fromtimestamp(os.path.getmtime(target_path))
            except Exception:
                return False 
                
            if attribute == "newer": return file_mtime > export_date
            if attribute == "older": return file_mtime < export_date
            
        return False

    @staticmethod
    def _is_date(value: str) -> bool:
        """Helper to check if a string is a date in YYYY-MM-DD format."""
        try:
            datetime.datetime.strptime(value, '%Y-%m-%d')
            return True
        except ValueError:
            return False

    @staticmethod
    def _get_date_from_text_content(project_path: str, patterns: List[str], regex_str: str, mode: str, rule: Dict[str, Any]) -> Union[str, None]:
        """Scrapes text content for a date or general text using regex."""
        regex = re.compile(regex_str or r'(\d{4}-\d{2}-\d{2})')
        detected_values = []
        
        for root, _, files in os.walk(project_path):
            for file_name in files:
                is_match = not patterns or any(fnmatch.fnmatch(file_name, p) for p in patterns)
                if not is_match: continue
                
                file_path = os.path.join(root, file_name)
                try:
                    with open(file_path, 'r', encoding='utf-8') as f:
                        content = f.read()
                        matches = regex.findall(content)
                        for match in matches:
                            detected_values.append(match[0] if isinstance(match, (tuple, list)) and match else match)
                except Exception: pass 
                
        if not detected_values: return None
        
        if rule.get("SubstitutionType", "String").lower() == "date":
            valid_dates = [v for v in detected_values if AttributeArbitrator._is_date(v)]
            if valid_dates and mode.lower() in ['lifo', 'descending']:
                return max(valid_dates, key=lambda x: datetime.datetime.strptime(x, '%Y-%m-%d'))
            elif valid_dates and mode.lower() in ['fifo', 'ascending']:
                return min(valid_dates, key=lambda x: datetime.datetime.strptime(x, '%Y-%m-%d'))
            return valid_dates[-1] if valid_dates else None
        
        return detected_values[-1] if detected_values else None


    @staticmethod
    def _get_attribute_value(
        source_attribute: str, 
        rule: Dict[str, Any], 
        project_path: str, 
        project_rel_path: str,
        path_root: str, 
        project_name: str, 
        cmd_args: Dict[str, str], 
        config_map: Dict[str, Dict[str, Any]]
    ) -> Union[str, None]:
        
        if source_attribute.lower() != 'derivevalue':
            return None

        # --- 1. Fixed Input (Highest Precedence) ---
        if "FixedValue" in rule:
            return str(rule["FixedValue"])

        # --- 2. Raw Input (Command Line, Environment Variable, or Path Segment Tokens) ---
        input_type = rule.get("RawInputType", "").lower()
        input_key = rule.get("InputKey")
        
        if input_type and input_key:
            raw_value = None
            
            if input_type == "commandlineargument" and input_key in cmd_args:
                raw_value = cmd_args[input_key]
            
            elif input_type == "environmentvariable":
                raw_value = os.getenv(input_key)
            
            if raw_value is not None:
                raw_value_str = str(raw_value).strip()
                raw_value_lower = raw_value_str.lower()

                if rule.get("SubstitutionType", "String").lower() == "date":
                    if raw_value_lower in ("now", "utc"):
                        return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
                    if raw_value_lower in ("today", "local"):
                        return datetime.date.today().strftime('%Y-%m-%d')
                    if AttributeArbitrator._is_date(raw_value_lower):
                        return raw_value_lower
                
                # RegEx Processing
                regex_str = rule.get("RegEx")
                if regex_str:
                    match = re.search(regex_str, raw_value_str)
                    if match:
                        return match.group(1) if len(match.groups()) > 0 else match.group(0)
                    return None

                return raw_value_str


        # --- 3. Path-Based Checks (Used primarily for Template Selection) ---
        path_check = rule.get("PathCheck", "").lower()
        
        if path_check == 'pathroot':
            config_types_to_check = rule.get("Pattern", [])
            for config_type in config_types_to_check:
                if path_root.lower() in [p.lower() for p in config_map.get(config_type.lower(), {}).get("Paths", [])]:
                    return config_type 
            return None 

        if path_check == 'projectname':
            regex_str = rule.get("RegEx")
            if regex_str:
                if re.match(regex_str, project_name):
                    return project_name
            return None 


        # --- 4. File Content/Metadata Search ---
        file_search_type = rule.get("FileSearchType", "").lower()
        patterns = rule.get("Pattern", [])

        if file_search_type == 'filecontentregex':
            regex_str = rule.get("RegEx")
            mode = rule.get("RegExMode", "LIFO")
            return AttributeArbitrator._get_date_from_text_content(project_path, patterns, regex_str, mode, rule)
        
        if file_search_type in ('filemodifiedtime', 'filecreatedtime'):
            latest_timestamp = None
            stat_func = os.path.getmtime if file_search_type == 'filemodifiedtime' else os.path.getctime
            
            try:
                if not patterns: return None
                
                for root, _, files in os.walk(project_path):
                    for file_name in files:
                        file_path = os.path.join(root, file_name)
                        if ".git" in file_path: continue
                        is_match = any(fnmatch.fnmatch(file_name, p) for p in patterns)
                        if not is_match: continue
                        
                        timestamp = stat_func(file_path)
                        current_date = datetime.datetime.fromtimestamp(timestamp).date()
                        if latest_timestamp is None or current_date > latest_timestamp: latest_timestamp = current_date
            except Exception: pass 
            return latest_timestamp.strftime('%Y-%m-%d') if latest_timestamp else None

        # --- 5. System Time (Lowest Precedence) ---
        time_type = rule.get("TimeType", "").lower()
        
        if time_type == 'utctime':
            return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
        if time_type == 'systemtime':
            return datetime.date.today().strftime('%Y-%m-%d')
        
        return None

    @staticmethod
    def determine_substitutions(
        project_path: str, 
        project_rel_path: str, 
        path_root: str,
        project_name: str,
        arbitration_configs: List[Dict[str, Any]],
        cmd_args: Dict[str, str],
        config_map: Dict[str, Dict[str, Any]]
    ) -> Dict[str, str]:
        
        substitution_rules = [
            rule for rule in arbitration_configs 
            if "Priority" in rule and "SubstitutionName" in rule and rule.get("SourceAttribute", "").lower() == "derivevalue"
        ]
        
        substitution_rules.sort(key=lambda x: x["Priority"])
        
        substitutions: Dict[str, str] = {}
        processed_sub_names = set()
        
        print("\nStarting Attribute/Substitution determination (sorted by Priority)...")
        
        for rule in substitution_rules:
            sub_name = rule["SubstitutionName"]
            priority = rule["Priority"]
            description = rule.get("Description", sub_name)
            
            if sub_name in processed_sub_names and sub_name not in ["project_config", "path_root", "project_name"]: continue

            extracted_value = AttributeArbitrator._get_attribute_value(
                rule["SourceAttribute"], rule, project_path, project_rel_path, 
                path_root, project_name, cmd_args, config_map
            )
            
            if extracted_value is None:
                if rule.get("Required", False):
                    print(f"\n🚨 ABORTING: REQUIRED substitution '{sub_name}' failed to derive a value (P{priority}).")
                    print(f"   -> Rule Description: {description}")
                    sys.exit(1)
                continue

            current_best_priority = float('inf')
            if sub_name in substitutions:
                for p_rule in substitution_rules:
                    if p_rule.get("SubstitutionName") == sub_name and p_rule.get("Priority") < current_best_priority:
                         current_best_priority = p_rule["Priority"]

                if priority < current_best_priority:
                    substitutions[sub_name] = extracted_value
                    processed_sub_names.add(sub_name)
                    print(f"✅ PRIORITY {priority}: {description} is authoritative {sub_name}: {extracted_value}")
                    continue

                if rule.get("Prompt"):
                    prompt_text = rule.get("PromptText", "Accept this value ({extracted_value}) (Y/n)? ")
                    prompt_text = prompt_text.replace("{extracted_value}", extracted_value).replace("{current_value}", substitutions[sub_name])
                    
                    question = f"\n⚠️ PRIORITY {priority}: {description} found: {extracted_value} (Interactive Prompt)\n"
                    question += f"Current best value (P{current_best_priority}) is: {substitutions[sub_name]}\n"
                    question += f"     {prompt_text} "

                    user_input = input(question).strip().lower()
                    
                    if user_input in ('y', ''):
                        substitutions[sub_name] = extracted_value
                        processed_sub_names.add(sub_name) 
                        print(f"✅ Accepted new value: {extracted_value}")
                        continue
                    else:
                        print(f"❌ Rejected. Keeping current best value.")
                        continue

                elif rule.get("AutoAccept", False):
                    substitutions[sub_name] = extracted_value
                    processed_sub_names.add(sub_name)
                    print(f"✅ PRIORITY {priority}: {description} found: {extracted_value} (AutoAccepted).")
                    continue
            
            else:
                substitutions[sub_name] = extracted_value
                processed_sub_names.add(sub_name)
                print(f"✅ PRIORITY {priority}: {description} found/set {sub_name}: {extracted_value}")
        
        return substitutions

    # --- File Conflict Logic ---
    
    @staticmethod
    def determine_actions_and_rule(
        target_path: str, 
        relative_path: str,
        arbitration_configs: List[Dict[str, Any]], 
        export_date_str: str
    ) -> Tuple[List[Dict[str, Any]], str, Union[Dict[str, Any], None], str]:
        """Determines file actions based on path, attributes, and priority rules."""
        
        try:
            export_date = datetime.datetime.strptime(export_date_str, '%Y-%m-%d').date()
        except ValueError:
            export_date = datetime.date.today()
            
        matching_rules = []
        
        # Filter for rules that are NOT ProgramFlow (only file-based arbitration rules)
        file_arbitration_rules = [
            r for r in arbitration_configs 
            if r.get("TargetAttribute") != ["ProgramFlow"]
            and not (isinstance(r.get("TargetAttribute"), list) and "ProgramFlow" in r.get("TargetAttribute"))
        ]
        
        for rule in file_arbitration_rules:
            if "TargetAttribute" in rule and "Actions" in rule and "Priority" in rule:
                
                attributes: Union[str, List[str]] = rule.get("TargetAttribute", [])
                patterns = rule.get("Pattern", [])
                
                if isinstance(attributes, str): attributes = [attributes.strip()] if attributes.strip() else []
                elif not isinstance(attributes, list): attributes: List[str] = []
                
                is_match = not patterns or any(fnmatch.fnmatch(relative_path, pattern) for pattern in patterns)
                if not is_match: continue

                attribute_met = True
                if attributes:
                    for attribute in attributes:
                        attribute = attribute.strip()
                        if not attribute: continue
                        if not AttributeArbitrator._check_file_attribute(attribute, target_path, export_date):
                            attribute_met = False
                            break
                
                if attribute_met:
                    matching_rules.append(rule)
            
        if not matching_rules:
            # Check the last MINIMAL_ATTRIBUTE_ARBITRATION rule which is the default skip rule
            default_skip_rule = [r for r in MINIMAL_ATTRIBUTE_ARBITRATION if r.get("TargetAttribute") == ["*"]][0]
            return default_skip_rule["Actions"], "Default Skip (No rule matched).", default_skip_rule, "Never"

        matching_rules.sort(key=lambda x: x["Priority"])

        concatenated_actions = []
        descriptions = []
        last_commit_condition = "Never" 
        
        for rule in matching_rules:
            concatenated_actions.extend(rule.get("Actions", []))
            descriptions.append(f"P{rule['Priority']}: {rule.get('Description', 'No description.')}")
            last_commit_condition = rule.get("Commit", last_commit_condition)
            
            if rule.get("Notify", False):
                attr_list = ', '.join([a for a in rule.get("TargetAttribute", []) if a])
                print(f"🚨 Conflict Rule Matched: [{attr_list or 'NO ATTRIBUTES'}] on '{relative_path}'")

        consolidated_description = " -> ".join(descriptions)

        return concatenated_actions, consolidated_description, matching_rules[-1], last_commit_condition


def _evaluate_commit_condition(condition: str, file_path: str, substitutions: Dict[str, Any]) -> bool:
    """Evaluates a string condition (e.g., '{export_date} > 2024-01-01') for the Git commit guard."""
    condition = condition.strip()

    if condition.lower() == "always": return True
    if condition.lower() == "never": return False
    
    if condition.lower() == "exists": return os.path.exists(file_path)

    condition_substituted = condition
    for placeholder, value in substitutions.items():
        if placeholder.startswith("{") and placeholder.endswith("}"): 
            condition_substituted = condition_substituted.replace(placeholder, str(value))
            
    now_utc_str = datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%d')
    condition_substituted = condition_substituted.replace("now()", f"'{now_utc_str}'")

    match = re.search(r'(.+?)\s*([<|>|==|=])\s*(.+)', condition_substituted, re.IGNORECASE)
    if not match: return False
        
    left_val_str, operator, right_val_str = [s.strip().strip("'\"") for s in match.groups()]
    
    # Date comparison
    try:
        left_date = datetime.datetime.strptime(left_val_str, '%Y-%m-%d').date()
        right_date = datetime.datetime.strptime(right_val_str, '%Y-%m-%d').date()
        
        if operator in ('>', '>>'): return left_date > right_date
        if operator in ('<', '<<'): return left_date < right_date
        if operator in ('=', '=='): return left_date == right_date
        return False
    except ValueError: pass
        
    # Number comparison
    try:
        left_num = float(left_val_str)
        right_num = float(right_val_str)
        
        if operator in ('>', '>>'): return left_num > right_num
        if operator in ('<', '<<'): return left_num < right_num
        if operator in ('=', '=='): return left_num == right_num
        return False
    except ValueError: pass
        
    # String equality
    if operator in ('=', '=='): return left_val_str.lower() == right_val_str.lower()
        
    return False

# --- Configuration and Argument Helper functions ---

def _validate_arbitration_priorities(config_map: Dict[str, Dict[str, Any]]):
    """
    Validates critical priority invariants across all loaded configurations.
    Enforces flow order and ensures file rules supersede default fallbacks.
    """
    print("\nStarting Priority Validation of Arbitration Rules...")
    

    for config_name, config in config_map.items():
        arbitration_rules = config["AttributeArbitration"]
        
        # --- 1. Program Flow Order Validation ---
        
        flow_rules = [
            rule for rule in arbitration_rules 
            if rule.get("TargetAttribute") == ["ProgramFlow"] or (isinstance(rule.get("TargetAttribute"), list) and "ProgramFlow" in rule.get("TargetAttribute"))
        ]
        
        create_dir_rule = next((r for r in flow_rules if r.get("Pattern", [""])[0] == "CreateDirectories"), None)
        file_trans_rule = next((r for r in flow_rules if r.get("Pattern", [""])[0] == "FileTransaction"), None)
        
        if create_dir_rule and file_trans_rule:
            create_dir_priority = create_dir_rule["Priority"]
            file_trans_priority = file_trans_rule["Priority"]
            
            if create_dir_priority >= file_trans_priority:
                print(f"🚨 CONFIGURATION ERROR in '{config_name}' template:")
                print(f"   ProgramFlow integrity failure: 'CreateDirectories' (P{create_dir_priority}) must have a LOWER priority number (higher precedence) than 'FileTransaction' (P{file_trans_priority}).")
                sys.exit(1)
        
        # --- 2. File Arbitration Integrity (vs. Default Skip Fallback) ---
        
        default_skip_rule = [r for r in MINIMAL_ATTRIBUTE_ARBITRATION if r.get("TargetAttribute") == ["*"]][0]
        default_skip_priority = default_skip_rule["Priority"]

        file_rules = [
            r for r in arbitration_rules 
            if r.get("TargetAttribute") != ["*"] # Not the default skip rule itself
            and r.get("TargetAttribute") != ["ProgramFlow"]
            and r.get("SourceAttribute") != "DeriveValue"
        ]
        
        for rule in file_rules:
            rule_priority = rule["Priority"]
            
            if rule_priority >= default_skip_priority:
                print(f"🚨 CONFIGURATION ERROR in '{config_name}' template:")
                print(f"   File Arbitration integrity failure: Rule '{rule.get('Description', 'Unnamed Rule')}' (P{rule_priority}) must have a priority number LESS THAN the default skip fallback (P{default_skip_priority}).")
                sys.exit(1)
                
    print("Priority validation passed.")


def _load_configs(template_root: str) -> Tuple[Dict[str, Dict[str, Any]], Union[Dict[str, Any], None]]:
    """Loads all template configurations into a map keyed by Type and merges default rules."""
    configs: Dict[str, Dict[str, Any]] = {}
    default_config = None
    
    if not os.path.exists(template_root):
        print(f"Error: Templates directory not found at '{template_root}'."); sys.exit(1)
    
    required_keys = ["Type", "Paths", "TemplateFiles", "DirectoriesToCreate"]
    
    for file_name in os.listdir(template_root):
        if file_name.endswith(".template"):
            config_path = os.path.join(template_root, file_name)
            try:
                with open(config_path, 'r') as f:
                    config = json.load(f)
                    
                    if not all(key in config for key in required_keys):
                        print(f"Error: Template file '{file_name}' is malformed (missing a required key)."); sys.exit(1)
                        
                    config["AttributeArbitration"] = config.get("AttributeArbitration", [])
                        
                    config_name = config["Type"].lower()
                    
                    configs[config_name] = config
                    if file_name.lower() == "default.template":
                        default_config = config
            except Exception as e:
                print(f"An unexpected error occurred reading config file {file_name}: {e}"); sys.exit(1)
                
    if not configs and not default_config: 
        print("Error: No valid project template configurations found (*.template files)."); sys.exit(1)
    
    default_arbitration_rules = MINIMAL_ATTRIBUTE_ARBITRATION
    
    if default_config:
        default_arb_base = [r for r in default_config["AttributeArbitration"] 
                            if r.get("Priority") not in [-10, -9, MAX_PRIORITY_INT]] 
        
        default_arbitration_rules = default_arb_base + [r for r in MINIMAL_ATTRIBUTE_ARBITRATION]
        default_config["AttributeArbitration"] = default_arbitration_rules
    
    for config_name, config in configs.items():
        if config_name == "default": continue 

        current_rules = config["AttributeArbitration"]
        
        # Merge by Priority/SubstitutionName to prevent duplicates
        filtered_defaults = [
            d_rule for d_rule in default_arbitration_rules 
            if not any(
                c_rule.get("Priority") == d_rule.get("Priority") 
                and c_rule.get("SubstitutionName") == d_rule.get("SubstitutionName")
                for c_rule in current_rules
            )
        ]
        
        config["AttributeArbitration"] = current_rules + filtered_defaults
        
    return configs, default_config

def _load_template_content(template_root: str, config_type: str, file_name: str) -> str:
    """Loads file content, looking first in the type-specific directory, then the root."""
    template_path = os.path.join(template_root, config_type, file_name)
    if os.path.exists(template_path):
        try:
            with open(template_path, 'r', encoding='utf-8') as f: return f.read()
        except:
            print(f"\nFATAL ERROR: Could not read content file: '{template_path}'"); sys.exit(1)
            
    template_path = os.path.join(template_root, file_name)
    if os.path.exists(template_path):
        try:
            with open(template_path, 'r', encoding='utf-8') as f: return f.read()
        except:
            print(f"\nFATAL ERROR: Could not read content file: '{template_path}'"); sys.exit(1)

    print(f"\nFATAL ERROR: Required template content file not found. Checked: '{os.path.join(template_root, config_type, file_name)}' and '{os.path.join(template_root, file_name)}'")
    sys.exit(1)

def _parse_cmd_arguments(args: List[str], project_rel_path: str) -> Dict[str, str]:
    """Tokenizes raw command-line arguments and project path segments into a unified dictionary."""
    parsed_args = {}
    i = 0
    p_index = 1
    
    # 1. Tokenize command line arguments and positional arguments
    while i < len(args):
        arg = args[i]
        if '=' in arg:
            key, value = arg.split('=', 1); key = key.lstrip('-').strip()
            if key: parsed_args[key] = value
            i += 1
            continue
        elif arg.startswith('-'):
            key = arg.lstrip('-').strip()
            if i + 1 < len(args) and not args[i+1].startswith('-'):
                parsed_args[key] = args[i+1]
                i += 2
            else:
                parsed_args[key] = ""
                i += 1
            continue
        else:
            parsed_args[f"positional_arg_{p_index}"] = arg
            p_index += 1
            i += 1
            
    # 2. Tokenize project path segments
    path_parts = project_rel_path.replace("\\", "/").split("/");
    for index, part in enumerate(path_parts):
        parsed_args[f"path_parts_{index}"] = part 
    
    if len(path_parts) < 1: parsed_args["path_parts_0"] = ""
    if len(path_parts) < 2: parsed_args["path_parts_1"] = ""
        
    return parsed_args

# --- Program Flow Execution Functions ---

def _program_flow_create_directories(
    project_path: str, 
    project_config: Dict[str, Any], 
    substitutions: Dict[str, str], 
    export_date_str: str
) -> None:
    """Handles the Directory Creation and Arbitration step."""
    
    directories = [os.path.join(project_path, d) for d in project_config.get("DirectoriesToCreate", [])]

    print("\n[ProgramFlow] Starting Directory Creation/Arbitration...")
    
    # Init Git Repo if it doesn't exist (must happen before directory/file actions)
    try:
        if not os.path.exists(os.path.join(project_path, ".git")):
            print(f"Initializing Git repository in '{project_path}'..."); 
            subprocess.run(["git", "init", project_path], check=True, capture_output=True, text=True)
        else:
            print(f"Git repository already exists in '{project_path}'. Skipping initialization.")
    except Exception as e:
        print(f"Error during Git init: {e}"); sys.exit(1)
        
    for directory_full_path in [project_path] + directories:
        directory_relative_path = directory_full_path.replace(project_path + os.sep, "", 1)
        rel_path_for_arb = directory_relative_path if directory_full_path != project_path else ""

        if os.path.exists(directory_full_path) and os.path.isdir(directory_full_path) and not os.access(directory_full_path, os.W_OK):
            
            actions, description, _, _ = AttributeArbitrator.determine_actions_and_rule(
                directory_full_path, 
                rel_path_for_arb, 
                project_config["AttributeArbitration"], 
                export_date_str
            )
            action = actions[0]["Action"] if actions and actions[0]["Action"] in ["Abort", "Warn"] else "Skip"

            if action == "Abort":
                custom_exit_code = actions[0].get("ExitCode") if actions else None
                exit_code = 1
                if custom_exit_code is not None:
                    try: exit_code = int(custom_exit_code)
                    except ValueError: pass
                    
                print(f"\n🚨 ABORTING: Directory write conflict on '{rel_path_for_arb}'. {description}")
                sys.exit(exit_code)
            elif action == "Warn":
                print(f"⚠️ WARNING: Directory '{rel_path_for_arb}' is not writable. Proceeding with 'Skip'. {description}")
        
        # Always attempt to create directories if not aborted
        os.makedirs(directory_full_path, exist_ok=True)
        if directory_full_path != project_path:
             print(f"Created directory: {directory_full_path}")


def _program_flow_file_transaction(
    project_path: str, 
    project_config: Dict[str, Any], 
    substitutions: Dict[str, str], 
    export_date_str: str,
    template_root: str
) -> None:
    """Handles the entire File Generation, Conflict Arbitration, and Final Write step."""
    
    print("\n[ProgramFlow] Starting File Generation and Arbitration...")
    
    files_to_create: Dict[str, str] = {}
    files_deleted: Set[str] = set()
    
    # 1. Arbitrate all files
    gitignore_content = ""
    project_type = project_config["Type"].lower()
    
    for file_config in project_config["TemplateFiles"]:
        source = file_config["Source"]
        destination_template = file_config["Destination"]
        
        destination = destination_template.format(**substitutions) 
        file_content = _load_template_content(template_root, project_type, source)
        
        if file_config.get("ContentRequiresSubstitution", False): 
            for placeholder, value in substitutions.items():
                if placeholder.startswith("{") and placeholder.endswith("}"): 
                    file_content = file_content.replace(placeholder, value)
        
        file_full_path = os.path.join(project_path, destination)
        relative_file_path = file_full_path.replace(project_path + os.sep, "", 1)

        if destination == ".gitignore":
             gitignore_content = file_content
             continue 

        if not os.path.exists(file_full_path):
            files_to_create[file_full_path] = file_content
            continue
            
        action_sequence, description, matching_rule, commit_condition = AttributeArbitrator.determine_actions_and_rule(
            file_full_path, 
            relative_file_path,
            project_config["AttributeArbitration"], 
            export_date_str
        )
        
        print(f"\nConflict detected for '{destination}'. Executing arbitration sequence...")
        print(f"  -> Match Description: {description}")
        
        actions_terminated = False
        action_outcome = None
        
        for step in action_sequence:
            if actions_terminated: break
                
            step_action = step.get("Action", "Skip")
            step_description = step.get("Description", f"Action: {step_action}")
            
            print(f"  -> Step: {step_action} ({step_description})")
            
            if step_action == "Skip":
                action_outcome = "Skip"; actions_terminated = True; break
            
            elif step_action == "Prompt":
                step_exit_code = step.get("ExitCode")
                step_prompt_text = step.get("PromptText", "Do you wish to proceed with the remainder of this sequence (Y/n)? ")
                
                question = f"     {step_prompt_text} "

                user_input = input(question).strip().lower()
                
                if user_input in ('y', ''):
                    if step_exit_code is not None:
                        try:
                            new_code = int(step_exit_code)
                            if new_code > 0: GLOBAL_STATE["max_warning_exit_code"] = max(GLOBAL_STATE["max_warning_exit_code"], new_code)
                        except ValueError: pass
                    continue
                else:
                    action_outcome = "Skip"; actions_terminated = True; break

            elif step_action == "Warn":
                custom_exit_code = step.get("ExitCode")
                if custom_exit_code is not None:
                    try: 
                        new_code = int(custom_exit_code)
                        if new_code > 0: GLOBAL_STATE["max_warning_exit_code"] = max(GLOBAL_STATE["max_warning_exit_code"], new_code)
                    except ValueError: pass 
                print(f"  -> WARNING. Continuing actions.")
                
            elif step_action == "Abort":
                action_outcome = "Abort"
                custom_exit_code = step.get("ExitCode")
                exit_code = 1
                if custom_exit_code is not None:
                    try: exit_code = int(custom_exit_code)
                    except ValueError: pass
                    
                print(f"\n🚨 ABORTING: Rule matched for file conflict on '{destination}'. {step_description}")
                sys.exit(exit_code)
            
            elif step_action == "Execute":
                # NOTE: This complex logic for command execution needs to be fully present here for the file-level actions
                command = step.get("Command", [])
                expected_codes = step.get("ExpectedExitCodes", [0])
                on_failure = step.get("OnFailure", "Abort").lower()

                substitutions_for_command = {**substitutions, "file_full_path": file_full_path, "{file_full_path}": file_full_path,}
                substituted_command = [arg.format(**substitutions_for_command) for arg in command]
                
                try:
                    result = subprocess.run(substituted_command, check=False, cwd=project_path, capture_output=True, text=True)
                    
                    if result.returncode not in expected_codes:
                        message = f"Execution failed (Exit Code: {result.returncode}). Stderr: {result.stderr.strip()}"
                        print(f"  -> FAILED: {message}. OnFailure: {on_failure.upper()}.")
                        
                        if on_failure == "abort": sys.exit(1)
                        elif on_failure == "skip": action_outcome = "Skip"; actions_terminated = True; break
                        elif on_failure == "replace": 
                            files_to_create[file_full_path] = file_content
                            action_outcome = "Replace"; actions_terminated = True; break
                            
                    else:
                        print(f"  -> Command executed successfully (Exit Code: {result.returncode}). Actions continuing.")
                except FileNotFoundError:
                    print(f"\n🚨 ABORTING: Command executable not found for '{destination}'. Command: {substituted_command[0]}")
                    sys.exit(1)
                except Exception as e:
                    print(f"\n🚨 ABORTING: Error executing command for '{destination}': {e}")
                    sys.exit(1)

            elif step_action in ["Delete", "Replace", "Append", "Prepend"]:
                
                if not _evaluate_commit_condition(commit_condition, file_full_path, substitutions):
                    print(f"  -> EXECUTION GUARD (Commit: '{commit_condition}') failed. Action '{step_action}' short-circuited to Skip.")
                    action_outcome = "Skip"; actions_terminated = True; break
                
                if step_action == "Delete":
                    try:
                        os.remove(file_full_path)
                        action_outcome = "Delete"; actions_terminated = True
                        files_deleted.add(file_full_path)
                        print(f"  -> File successfully deleted.")
                        break
                    except Exception as e:
                        print(f"Error deleting file {destination}: {e}. Actions terminated with Skip.")
                        action_outcome = "Skip"; actions_terminated = True; break

                elif step_action == "Replace":
                    files_to_create[file_full_path] = file_content
                    action_outcome = "Replace"; actions_terminated = True
                    print(f"  -> File marked for Replace/Write.")
                    break
                    
                elif step_action == "Append":
                    try:
                        with open(file_full_path, 'r', encoding='utf-8') as f: old_content = f.read()
                        new_content = old_content.rstrip() + "\n" + file_content.lstrip()
                        files_to_create[file_full_path] = new_content
                        action_outcome = "Append"; actions_terminated = True
                        print(f"  -> File marked for Append/Write.")
                        break
                    except Exception as e:
                        print(f"Error appending to {destination}: {e}. Actions terminated with Skip.")
                        action_outcome = "Skip"; actions_terminated = True; break

                elif step_action == "Prepend":
                    try:
                        with open(file_full_path, 'r', encoding='utf-8') as f: old_content = f.read()
                        new_content = file_content.rstrip() + "\n" + old_content.lstrip()
                        files_to_create[file_full_path] = new_content
                        action_outcome = "Prepend"; actions_terminated = True
                        print(f"  -> File marked for Prepend/Write.")
                        break
                    except Exception as e:
                        print(f"Error prepending to {destination}: {e}. Actions terminated with Skip.")
                        action_outcome = "Skip"; actions_terminated = True; break
            
            else:
                print(f"  -> UNKNOWN action '{step_action}' in actions step. Continuing actions.")
                
        if action_outcome not in ["Replace", "Append", "Prepend", "Delete"]:
            print(f"  -> Final outcome: Skip. File not modified/deleted.")
                
    # 2. Handle .gitignore
    if not gitignore_content: gitignore_content = "# No project-specific .gitignore provided in template."
    if os.path.exists(os.path.join(project_path, "venv")): gitignore_content += "\n# Virtual Environment\nvenv/\n"
    gitignore_path = os.path.join(project_path, ".gitignore")
    files_to_create[gitignore_path] = gitignore_content
    
    # 3. Write/Delete Files
    print("\n[ProgramFlow] Writing/Deleting files...")
    for file_path, content in files_to_create.items():
        os.makedirs(os.path.dirname(file_path), exist_ok=True) 
        status = "Created" if not os.path.exists(file_path) else "Modified/Replaced"
        with open(file_path, "w", encoding='utf-8', newline='\n') as f: f.write(content)
        print(f"{status} file: {os.path.basename(file_path)}")
        
    for file_path in files_deleted:
        # Note: Delete action already handles os.remove. This is for Git staging preparation.
        pass 

    # 4. Update GLOBAL_STATE for subsequent flow actions
    GLOBAL_STATE["files_to_create"] = files_to_create
    GLOBAL_STATE["files_deleted"] = files_deleted


def _program_flow_execute_actions(
    actions: List[Dict[str, Any]], 
    substitutions: Dict[str, str], 
    project_path: str
) -> int:
    """Executes a list of actions (Execute, Warn, etc.) for ProgramFlow rules."""
    max_exit_code = 0
    has_changes = bool(GLOBAL_STATE["files_to_create"] or GLOBAL_STATE["files_deleted"])
    
    # Check if any git command is configured, and if so, stage files first.
    is_git_step = any("git" in ' '.join(step.get("Command", [])).lower() for step in actions if step.get("Action") == "Execute")
    
    if is_git_step:
         if not has_changes:
             print("[ProgramFlow] Skipping Git Actions: No files were created, modified, or deleted.")
             return 0
             
         try:
             print("\n[ProgramFlow] Preparing files for configurable Git operations...")
             for file_path in GLOBAL_STATE["files_to_create"].keys():
                 subprocess.run(["git", "add", file_path], check=True, cwd=project_path, capture_output=True, text=True)
                 
             for file_path in GLOBAL_STATE["files_deleted"]:
                 subprocess.run(["git", "rm", "-f", file_path], check=True, cwd=project_path, capture_output=True, text=True)
         except Exception as e:
              print(f"Warning: Failed to stage files for commit. Execution may fail. Error: {e}")
    
    for step in actions:
        action_type = step.get("Action", "Execute").lower()
        description = step.get("Description", action_type)
        
        if action_type == "execute":
            command = step.get("Command", [])
            expected_codes = step.get("ExpectedExitCodes", [0])
            on_failure = step.get("OnFailure", "Abort").lower()

            # Handle multi-line message substitution for the git commit -F flag dynamically
            substituted_command = []
            temp_file_path = None
            if len(command) >= 3 and command[1] == "commit" and command[2] in ["-m", "-F"]:
                commit_message = substitutions.get("export_commit_message", "Initialization commit (default fallback)").format(**substitutions)
                
                if "\n" in commit_message.strip():
                    temp_file_path = os.path.join(project_path, ".git_commit_msg.tmp")
                    with open(temp_file_path, "w", encoding='utf-8', newline='\n') as f: f.write(commit_message)
                    substituted_command = [command[0], command[1], "-F", temp_file_path] 
                else:
                    substituted_command = [command[0], command[1], "-m", commit_message]
            else:
                substituted_command = [arg.format(**substitutions) for arg in command]

            print(f"\n⚙️ Executing ProgramFlow Action: {description} (Command: {' '.join(substituted_command)})")
            
            try:
                result = subprocess.run(substituted_command, check=False, cwd=project_path, capture_output=True, text=True)
                
                if result.returncode not in expected_codes:
                    message = f"Execution failed (Exit Code: {result.returncode}). Stderr: {result.stderr.strip()}"
                    print(f"  -> FAILED: {message}. OnFailure: {on_failure.upper()}.")
                    
                    if on_failure == "abort": sys.exit(1)
                    elif on_failure == "warn": 
                        custom_exit_code = step.get("ExitCode", 1)
                        try: max_exit_code = max(max_exit_code, int(custom_exit_code))
                        except ValueError: pass
                        continue
                        
                else:
                    print(f"  -> Command executed successfully (Exit Code: {result.returncode}).")
            except FileNotFoundError:
                print(f"\n🚨 ABORTING: Command executable not found for '{description}'. Command: {substituted_command[0]}")
                sys.exit(1)
            except Exception as e:
                print(f"\n🚨 ABORTING: Error executing command for '{description}': {e}")
                sys.exit(1)
            finally:
                if temp_file_path and os.path.exists(temp_file_path): os.remove(temp_file_path)

        elif action_type == "warn":
             print(f"⚠️ PROGRAM FLOW WARNING: {description}")
             custom_exit_code = step.get("ExitCode")
             if custom_exit_code is not None:
                try: 
                    new_code = int(custom_exit_code)
                    if new_code > 0: max_exit_code = max(max_exit_code, new_code)
                except ValueError: pass 

        elif action_type == "abort":
            print(f"\n🚨 ABORTING: Program Flow Abort Action: {description}")
            sys.exit(1)

    return max_exit_code

# --- Main Logic ---
def initialize_project(cli_args: List[str]):
    
    if len(cli_args) < 2:
        print("Error: A relative project path argument is required (e.g., Apps/MyProject)."); sys.exit(1)
    
    project_rel_path = cli_args[1]
    raw_cmd_args = cli_args[2:]
    
    kinetic_dev_root = os.getenv("KINETIC_DEV_ROOT")
    if not kinetic_dev_root:
        print("Error: KINETIC_DEV_ROOT environment variable is not set."); sys.exit(1)
    
    template_root = os.path.join(kinetic_dev_root, "templates")
    CONFIGS_MAP, DEFAULT_CONFIG = _load_configs(template_root) 
    
    # Run Priority Validation on All Configs
    _validate_arbitration_priorities(CONFIGS_MAP)
    
    cmd_args_dict = _parse_cmd_arguments(raw_cmd_args, project_rel_path)
    print(f"Parsed Command Line Arguments (Tokenized): {cmd_args_dict}")

    # --- 1. Arbitrate for Core Path Components and Project Configuration ---
    substitutions_dict = AttributeArbitrator.determine_substitutions(
        project_path="", project_rel_path=project_rel_path, path_root="", project_name="",
        arbitration_configs=DEFAULT_CONFIG["AttributeArbitration"], cmd_args=cmd_args_dict, config_map=CONFIGS_MAP
    )
    
    path_root = substitutions_dict.get("path_root")
    project_name = substitutions_dict.get("project_name")
    
    project_path = os.path.join(kinetic_dev_root, path_root, project_name)

    if not os.path.exists(project_path):
        print(f"Error: The derived project directory '{project_path}' does not exist.")
        sys.exit(1)
    
    # --- 2. Select the final config and re-arbitrate for ALL substitutions ---
    
    config_type_key = substitutions_dict.get("project_config", "default").lower()
    project_config = CONFIGS_MAP.get(config_type_key, DEFAULT_CONFIG)

    project_type = project_config["Type"].lower()
    print(f"Selected project configuration: '{project_type}'.")

    substitutions_dict_final = AttributeArbitrator.determine_substitutions(
        project_path=project_path, project_rel_path=project_rel_path, path_root=path_root,
        project_name=project_name, arbitration_configs=project_config["AttributeArbitration"], 
        cmd_args=cmd_args_dict, config_map=CONFIGS_MAP
    )
    
    export_date_str = substitutions_dict_final.get("export_date", datetime.date.today().strftime('%Y-%m-%d'))
    
    substitutions = {
        "project_name": project_name, 
        "project_path": project_path.replace(kinetic_dev_root + os.sep, ""),
        **substitutions_dict_final
    }
    for key, value in substitutions.copy().items():
        substitutions["{" + key + "}"] = value

    # --- 3. Gather and Execute ProgramFlow Actions ---
    
    flow_rules = [
        rule for rule in project_config["AttributeArbitration"] 
        if rule.get("TargetAttribute") == ["ProgramFlow"] or (isinstance(rule.get("TargetAttribute"), list) and "ProgramFlow" in rule.get("TargetAttribute"))
    ]
    flow_rules.sort(key=lambda x: x["Priority"])

    overall_max_warning_code = 0
    
    print("\nStarting Program Flow Execution (sorted by Priority)...")
    
    for rule in flow_rules:
        step_pattern = rule.get("Pattern", [""])[0] 
        actions = rule.get("Actions", [])
        
        if step_pattern == "CreateDirectories":
            _program_flow_create_directories(project_path, project_config, substitutions, export_date_str)
        
        elif step_pattern == "FileTransaction":
            _program_flow_file_transaction(project_path, project_config, substitutions, export_date_str, template_root)
            overall_max_warning_code = max(overall_max_warning_code, GLOBAL_STATE["max_warning_exit_code"])
            
        elif step_pattern == "PostInitCommit" or step_pattern == "ExecuteGeneral":
            final_warning_code = _program_flow_execute_actions(actions, substitutions, project_path)
            overall_max_warning_code = max(overall_max_warning_code, final_warning_code)

        else:
            print(f"\n⚠️ WARNING: Unknown ProgramFlow pattern '{step_pattern}' encountered (P{rule['Priority']}). Skipping rule.")

    
    print(f"\n✅ Project '{project_name}' configured successfully in '{project_path}'.")

    if overall_max_warning_code > 0:
        print(f"\n⚠️ WARNINGS were encountered during execution. Exiting with highest warning code: {overall_max_warning_code}")
        sys.exit(overall_max_warning_code)
    

if __name__ == "__main__":
    try:
        initialize_project(sys.argv)
    except subprocess.CalledProcessError as e:
        print(f"Error during command: {e.cmd} - {e.returncode}"); print(f"Stderr: {e.stderr}"); sys.exit(1)
    except Exception as e:
        print(f"An unexpected error occurred: {e}"); sys.exit(1)
import unittest
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from methods.shell_assignments import VARIABLE_ASSIGNMENT_PATTERN, VARIABLE_REFERENCE_PATTERN


class TestVariableAssignmentRegex(unittest.TestCase):
    def setUp(self):

        self.variable_regex = VARIABLE_ASSIGNMENT_PATTERN

        # Simple test cases are used in both test methods
        self.simple_test_cases = {
            "var=value": "var=value",
            "Var=value": "Var=value",
            "VAR=value": "VAR=value",
            "var123=123": "var123=123",
            "v_=true": "v_=true",
            "_user='John Doe'": "_user='John Doe'",
            "a1=\"1.0.0\" || echo 'version error'": "a1=\"1.0.0\"",
            "   _2 =  value2": "_2 =  value2",
            "long_variable_name=(1 2 3 4)": "long_variable_name=(1 2 3 4)",
            "max_num=$((3+5))": "max_num=$((3+5))",
            "export MIN_NUM=1": "export MIN_NUM=1",  # Updated expectation if excluding 'export'
            "echo 'var=notthisone'": None,
            "# Commented_var=notthis": None,
            "\"varname=invalid\"": None,
            "echo 'Setting X=5'": None,
            "count+=1": "count+=1",
            "_var_ok=ok": "_var_ok=ok",
            "VAR-NAME=invalid": None,  # '-' is not allowed in variable names
            "declare -i Var=value": "declare -i Var=value",
            "export VAR=value": "export VAR=value",
            "declare -xr var123=123": "declare -xr var123=123",
            "THIS_DIR=\"$(dirname \"$0\")\"": "THIS_DIR=\"$(dirname \"$0\")\"",
            "local v_=true": "local v_=true",
            "local _user='John Doe'": "local _user='John Doe'",
            "function my_func() { local loc_var=inside; }": "local loc_var=inside",
            'THIS_FILE="$BASH_SOURCE"': 'THIS_FILE="$BASH_SOURCE"'
        }

        self.complex_test_cases = {
            "export LONG_VAR=\"value that \\\ncontinues on the next line\"": None,
            "var=$(echo $(date) | cut -d' ' -f1)": "var=$(echo $(date) | cut -d' ' -f1)",
            "var=\"$(echo $(date) | cut -d' ' -f1)\"": "var=\"$(echo $(date) | cut -d' ' -f1)\"",
            "if [ condition ]; then VAR=\"value\"; else VAR=\"other\"; fi": "VAR=\"value\"",
            "[[ condition ]] && VAR=value": "VAR=value",
            "VAR=\"Complex \\\"inner quote\\\" scenario\"": "VAR=\"Complex \\\"inner quote\\\" scenario\"",
            "VAR=value # this is a comment": "VAR=value",
            "arr=( item1 item2 item3 )": "arr=( item1 item2 item3 )",
            "VAR=value; ANOTHER_VAR=another_value": "VAR=value",
            "# var=is_a_comment": None,
            "echo 'This is # not a comment'; var=\"test\"": "var=\"test\"",
            "echo \"This is # not a comment\"; var=\"test\"": "var=\"test\"",
            "echo 'This is a string && var=test'": None,
            "echo \\\"This is an escaped string; var=test\\\"": None,
            r'echo "$(var="test")"': 'var="test"',
            "\"var=is_in_quotes\"": None,
            "\"$(var=is_in_command_quotes)\"": "var=is_in_command_quotes",
            "echo var=false": None,
            "'$(var=is_in_single_quotes)'": None,
            'echo $(var="$SOME_PATH"; ls $SOME_PATH)': 'var="$SOME_PATH"'
        }

    def test_variable_assignments(self):
        """Test complex pattern, handling both simple and complex scenarios."""
        # Merge simple cases with additional complex cases
        test_cases = {
            **self.simple_test_cases,
            **self.complex_test_cases,
        }

        for case, expected in test_cases.items():
            with self.subTest(case=case):
                match = self.variable_regex.match(case)
                result = ''.join(filter(lambda m: bool(m), match.groups())) if match else None
                self.assertEqual(result, expected)


class TestVariableReference(unittest.TestCase):
    def setUp(self):
        # Regular expression to match bash variable reference
        self.variable_regex = VARIABLE_REFERENCE_PATTERN

    def test_variables(self):
        test_cases = {
            # Basic variable usage
            "$BASH_SOURCE and ${any_command}": [('$BASH_SOURCE', ''),
                                                ('${any_command}', '')],
            "No variables here!": [],

            # Quoting behavior
            "'Single quotes $NO_VAR'": [],
            '"Double quotes with $VAR inside"': [('$VAR', '')],
            'Mixed usage $VAR1, "${VAR2}" and \'$VAR3\'': [('$VAR1', ''),
                                                           ('${VAR2}', '')],

            # Nested quotes and escapes
            '"Nested "quotes" with $VAR1 and ${VAR2}"': [('$VAR1', ''),
                                                         ('${VAR2}', '')],

            # Multiple variables with various quotations
            '$VAR1 "$VAR2" \'$VAR3\' "${VAR4}" \'${VAR5}\' $VAR6': [('$VAR1', ''),
                                                                    ('$VAR2', ''),
                                                                    ('${VAR4}', ''),
                                                                    ('$VAR6', '')],

            # Variables next to punctuation
            "$VAR1,$VAR2.$VAR3:$VAR4;$VAR5": [('$VAR1', ''),
                                              ('$VAR2', ''),
                                              ('$VAR3', ''),
                                              ('$VAR4', ''),
                                              ('$VAR5', '')],

            # Escaped dollar signs and backslashes
            r"$REAL_VAR but \$ESCAPED and \\$ALSO_REAL": [('$REAL_VAR', ''),
                                                          ('$ALSO_REAL', '')],
            r'$UNESCAPED "\$ESCAPED_IN_DOUBLE" \'$SINGLE\' "\\$ESCAPED_BACKSLASH"': [('$UNESCAPED', ''),
                                                                                     ('$ESCAPED_BACKSLASH', '')],

            # Variables in command substitution
            'Result: $(echo $INSIDE_COMMAND) and ${OUTSIDE}': [('$INSIDE_COMMAND', ''),
                                                               ('${OUTSIDE}', '')],

            # Variable in quoted command substitution
            'cd "$(dirname "$BASH_SOURCE")" || exit 1': [('$BASH_SOURCE', '')],

            # Variables in arithmetic expansion
            'Total: $(($VAR1 + ${VAR2})) items': [('$VAR1', ''),
                                                  ('${VAR2}', '')],

            # Variables with special characters in braces
            "${VAR_1} ${VAR-2} ${VAR_3-DEFAULT} ${VAR_4:-OTHER}": [('${VAR_1}', ''),
                                                                   ('${VAR-2}', ''),
                                                                   ('${VAR_3-DEFAULT}', ''),
                                                                   ('${VAR_4:-OTHER}', '')],

            # Variables next to word boundaries
            "word$VAR $VARword $VAR_word": [('$VAR', ''),
                                            ('$VARword', ''),
                                            ('$VAR_word', '')],

            # Variables in here-documents (simulated)
            '''<<EOF
                    This $SHOULD_MATCH
                    This '$SHOULD_NOT'
                    This "$ALSO_SHOULD"
                EOF''': [('$SHOULD_MATCH', ''),
                         ('$ALSO_SHOULD', '')],

            # Multiple backslashes
            r"\\$VAR1 \\\$VAR2 \\\\$VAR3 \\\\\$VAR4": [('$VAR1', ''),
                                                       ('$VAR3', '')],
            r"$VAR1 \$VAR2 \\$VAR3 \\\$VAR4 \\\\$VAR5": [('$VAR1', ''),
                                                         ('$VAR3', ''),
                                                         ('$VAR5', '')],

            # Variables in backticks (old-style command substitution)
            "Result: `echo $INSIDE_BACKTICKS` and $OUTSIDE": [('$INSIDE_BACKTICKS', ''),
                                                              ('$OUTSIDE', '')],

            # Variables with numbers
            "$VAR1 $VAR2 $VAR3 $VAR10 $VAR_11": [('$VAR1', ''),
                                                 ('$VAR2', ''),
                                                 ('$VAR3', ''),
                                                 ('$VAR10', ''),
                                                 ('$VAR_11', '')],

            # Variables with underscores and numbers
            "$_VAR $VAR_ $_VAR_ $_1VAR $VAR_2_": [('$_VAR', ''),
                                                  ('$VAR_', ''),
                                                  ('$_VAR_', ''),
                                                  ('$_1VAR', ''),
                                                  ('$VAR_2_', '')],

            # Complex brace expansion
            "${VAR:-default} ${VAR:=default} ${VAR:?error} ${VAR:+alt_value}": [('${VAR:-default}', ''),
                                                                                ('${VAR:=default}', ''),
                                                                                ('${VAR:?error}', ''),
                                                                                ('${VAR:+alt_value}', '')],

            # Variables in array indices
            "${array[$INDEX]} ${array[${INSIDE}]} $array[0]": [('${array[$INDEX]}', '$INDEX'),
                                                               ('${array[${INSIDE}]}', '${INSIDE}'),
                                                               ('$array', '')],

            # Variables in shell command groups
            'echo \"$(var="$SOME_PATH"; ls $var)\"': [('$SOME_PATH', ''),
                                                      ('$var', '')]
        }

        for test_case, expected in test_cases.items():
            with self.subTest(test_case=test_case):
                matches = self.variable_regex.findall(test_case)
                self.assertEqual(matches, expected)


if __name__ == '__main__':
    unittest.main()

#!/usr/bin/env python3
"""
Test Python shebang in /usr/bin/ binary
"""
import sys

ret = 0
bin_path = "/usr/bin/{}".format(sys.argv[1])
shebang = "#!/usr/bin/{}".format(sys.argv[2])

with open(bin_path) as f:
    first_line = f.readline().rstrip().replace(" ", "")
    if first_line != shebang:
        print("ERROR: shebang '{}' not found in {}".format(shebang, bin_path))
        ret = 1
    else:
        print("OK")

sys.exit(ret)

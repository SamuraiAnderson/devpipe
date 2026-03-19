import re

def abspath(pwd, path, os_name='posix'):
    # Split the path into parts
    if os_name == 'posix':
        parts = path.split('/')
        abs_parts = pwd.split('/')
        sep = '/'
    else:  # windows
        parts = re.split(r'\\|/', path)
        abs_parts = re.split(r'\\|/', pwd)
        sep = '\\'

    # Handle each part of the path
    for part in parts:
        if part == '.':
            # Ignore the current directory
            continue
        elif part == '..':
            # Go up one directory level if possible
            if abs_parts:
                abs_parts.pop()
        else:
            # Add the part to the absolute path
            abs_parts.append(part)
    
    return sep.join(abs_parts)

def isabs_windows(path):
    if path.startswith('/') or path.startswith('\\'):
        return False
    if len(path) >= 3 and path[1] == ':' and (path[2] == '/' or path[2] == '\\'):
        return True
    if len(path) >= 2 and (path.startswith('//') or path.startswith('\\\\')):
        return True
    return False

if __name__ == "__main__":
    print("C:\\Projects\\Controls", "..\\src", abspath("C:\\Projects\\Controls", "..\\src", 'nt'))
    print("/data/bin", "../toolchain/src", abspath("/data/bin", "../src"))
    print("/data/bin", "src", abspath("/data/bin", "src"))
    print("/data/bin", "./src", abspath("/data/bin", "./src"))
    print("/data/bin", "./src", abspath("/data/bin", "./../src"))

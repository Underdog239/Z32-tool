local s = "hello world"
print(string.upper(s), string.lower(s))
print(string.sub(s, 1, 5), string.sub(s, -5))
print(string.find(s, "world"))
print(string.rep("ab", 3))
print(string.len(s), #s)
print(("%d-%s-%.2f"):format(42, "x", 1.5))
print(string.byte("A"), string.char(65, 66))
print(("a,b,c"):gsub(",", ";"))
for w in string.gmatch("one two three", "%a+") do print(w) end
print(tostring(12), tonumber("34"), tonumber("z"), tonumber("ff", 16))
print(("trim me"):match("^%s*(.-)%s*$"))
print(("%5d|"):format(7), ("%-5d|"):format(7))
print("escapes: \65\66\67 \116\97\98")
print([[
long
string]])

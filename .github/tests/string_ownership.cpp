#include "4DPlugin-JSON.h"
#include "PrivateTypes.h"
#include "EntryPoints.h"
#include "ARRAY_TEXT.h"
#include "C_TEXT.h"
#include <cassert>
#include <cstdlib>
#include <cstring>
#include <set>

// Only engine services are simulated. All PA_* functions come from the full SDK.
static bool allocatedEmpty;
static std::set<PA_Unichar *> strings;
static std::map<PA_Handle, size_t> handles;
struct Reference {
    size_t owners = 1;
    std::map<CUTF16String, PA_Variable> properties;
};
static std::set<Reference *> references;
static bool stringifyMismatch;
static bool failResize;

static CUTF16String text(const PA_Unistring &s) {
    PA_Unichar *buffer = s.fString;
    assert(s.fLength >= 0);
    if (!s.fString) {
        assert(s.fLength == 0);
        return {};
    }
    assert(strings.count(buffer));
    assert(s.fString[s.fLength] == 0);
    return CUTF16String(s.fString, s.fLength);
}

static PA_Unistring makeString(const CUTF16String &s) {
    PA_Unistring result = {};
    result.fLength = static_cast<PA_long32>(s.size());
    if (!s.empty() || allocatedEmpty) {
        result.fString = static_cast<PA_Unichar *>(calloc(s.size() + 1, sizeof(PA_Unichar)));
        assert(result.fString);
        memcpy(result.fString, s.c_str(), s.size() * sizeof(PA_Unichar));
        PA_Unichar *buffer = result.fString;
        assert(strings.insert(buffer).second);
    }
    return result;
}

static void dispose(PA_Unistring &s) {
    if (s.fString) {
        PA_Unichar *buffer = s.fString;
        assert(strings.erase(buffer) == 1);
        free(s.fString);
    }
    s = {};
}

static Reference *reference() {
    auto *r = new Reference;
    references.insert(r);
    return r;
}

static void clear(PA_Variable &v);
static void release(void *p) {
    if (!p) return;
    auto *r = static_cast<Reference *>(p);
    assert(references.count(r) && r->owners);
    if (--r->owners == 0) {
        for (auto &entry : r->properties) clear(entry.second);
        references.erase(r);
        delete r;
    }
}

static PA_Variable copy(PA_Variable v) {
    if (v.fType == eVK_Unistring) v.uValue.fString = makeString(text(v.uValue.fString));
    if (v.fType == eVK_Object || v.fType == eVK_Collection) {
        auto *r = static_cast<Reference *>(v.uValue.fObject);
        if (r) {
            assert(references.count(r));
            ++r->owners;
        }
    }
    return v;
}

static void clear(PA_Variable &v) {
    if (v.fType == eVK_Unistring) dispose(v.uValue.fString);
    if (v.fType == eVK_Object || v.fType == eVK_Collection) release(v.uValue.fObject);
    if (v.fType == eVK_ArrayUnicode) {
        auto &a = v.uValue.fArray;
        PA_Handle h = a.fData;
        assert(handles.at(h) >= (a.fNbElements + 1) * sizeof(PA_Unistring));
        auto *data = reinterpret_cast<PA_Unistring *>(*a.fData);
        for (PA_long32 i = 0; i <= a.fNbElements; ++i) dispose(data[i]);
        PA_DisposeHandle(a.fData);
    }
    v = {};
}

static void engine(short selector, EngineBlock *eb) {
    eb->fError = eER_NoErr;
    switch (selector) {
        case EX_CREATE_UNISTRING:
            eb->fUniString1 = makeString(CUTF16String(reinterpret_cast<PA_Unichar *>(eb->fHandle)));
            return;
        case EX_DISPOSE_UNISTRING:
            dispose(eb->fUniString1);
            return;
        case EX_SET_UNISTRING: {
            CUTF16String incoming(reinterpret_cast<PA_Unichar *>(eb->fHandle));
            dispose(eb->fUniString1);
            eb->fUniString1 = makeString(incoming);
            return;
        }
        case EX_CLEAR_VARIABLE:
            clear(*static_cast<PA_Variable *>(eb->fPtr1));
            return;
        case EX_VARIABLE_TO_STRING: {
            auto *v = reinterpret_cast<PA_Variable *>(eb->fHandle);
            assert(v->fType == eVK_Unistring);
            text(v->uValue.fString);
            eb->fUniString1 = v->uValue.fString;
            return;
        }
        case EX_HANDLE_MANAGER: {
            PA_Handle h = eb->fParam1 == 1 ? nullptr : eb->fHandle;
            sLONG_PTR size = (eb->fParam1 == 1 || eb->fParam1 == 3) ? eb->fParam2 : 0;
            switch (eb->fParam1) {
                case 1: {
                    auto h = static_cast<PA_Handle>(malloc(sizeof(char *)));
                    *h = static_cast<char *>(calloc(1, std::max<sLONG_PTR>(1, size)));
                    handles[h] = eb->fParam2;
                    eb->fHandle = h;
                    return;
                }
                case 2:
                    assert(handles.erase(h) == 1);
                    free(*eb->fHandle);
                    free(eb->fHandle);
                    return;
                case 3:
                    assert(handles.count(h));
                    if (failResize) {
                        eb->fError = 1;
                        return;
                    }
                    *h = static_cast<char *>(realloc(*h, std::max<sLONG_PTR>(1, size)));
                    handles[h] = size;
                    return;
                case 4: eb->fParam2 = handles.at(h); return;
                case 5: eb->fParam3 = reinterpret_cast<sLONG_PTR>(*eb->fHandle); return;
                case 6: return;
                default: assert(false);
            }
        }
        case EX_SET_OBJ_VALUE: {
            assert(eb->fShort == eVK_Object);
            auto *r = static_cast<Reference *>(eb->fPtr1);
            assert(references.count(r));
            auto &dest = r->properties[text(eb->fUniString1)];
            PA_Variable incoming = copy(*static_cast<PA_Variable *>(eb->fPtr2));
            clear(dest);
            dest = incoming;
            return;
        }
        case EX_GET_OBJ_VALUE: {
            assert(eb->fShort == eVK_Object);
            auto *r = static_cast<Reference *>(eb->fPtr1);
            assert(references.count(r));
            *static_cast<PA_Variable *>(eb->fPtr2) = copy(r->properties.at(text(eb->fUniString1)));
            return;
        }
        case EX_EXECUTE_COMMAND_BY_ID: {
            auto **args = static_cast<PA_Variable **>(eb->fPtr1);
            auto *result = static_cast<PA_Variable *>(eb->fPtr2);
            if (eb->fParam1 == 1231) {
                auto *r = static_cast<Reference *>(args[0]->uValue.fObject);
                assert(references.count(r));
                PA_SetBooleanVariable(result, r->properties.count(text(args[1]->uValue.fString)) != 0);
            } else if (eb->fParam1 == 1217) {
                if (stringifyMismatch) PA_SetObjectVariable(result, reference());
                else {
                    auto s = makeString(CUTF16String({'{', '}', 0x00e9, 0xd83d, 0xde00}));
                    PA_SetStringVariable(result, &s);
                }
            } else if (eb->fParam1 == 1225) {
                auto *src = static_cast<Reference *>(args[0]->uValue.fObject);
                auto *dest = reference();
                for (auto &entry : src->properties) dest->properties[entry.first] = copy(entry.second);
                PA_SetObjectVariable(result, dest);
            } else {
                assert(false && "unexpected command");
            }
            return;
        }
        default: assert(false && "unexpected engine selector");
    }
}

extern "C" void PluginMain(PA_long32, PA_PluginParameters) {}

static CUTF16String property(Reference *r, const CUTF16String &key) {
    return text(r->properties.at(key).uValue.fString);
}

static void setters(int selected) {
    const wchar_t *wide[] = {L"ASCII", L"\u00e9\u6f22", L"\U0001f600", L""};
    const char *utf8[] = {"ASCII", "\xc3\xa9\xe6\xbc\xa2", "\xf0\x9f\x98\x80", ""};
    const CUTF16String exact[] = {{'A','S','C','I','I'}, {0x00e9,0x6f22}, {0xd83d,0xde00}, {}};
    for (size_t i = 0; i < 4; ++i) {
        for (int overload = 0; overload < 4; ++overload) {
            if (overload != selected) continue;
            auto *r = reference();
            for (int repeat = 0; repeat < 3; ++repeat) {
                if (overload == 0) ob_set_s(r, "key", utf8[i]);
                if (overload == 1) ob_set_s(r, L"key", utf8[i]);
                if (overload == 2) ob_set_a(r, L"key", wide[i]);
                if (overload == 3) ob_set_a(r, L"key", &exact[i]);
                assert(property(r, {'k','e','y'}) == exact[i]);
            }
            release(r);
            if (!strings.empty()) {
                std::cerr << "setter overload " << overload << " leaked " << strings.size()
                          << " SDK strings (allocatedEmpty=" << allocatedEmpty << ")\n";
                abort();
            }
        }
    }
}

static void getters() {
    auto *r = reference();
    CUTF8String output;
    const CUTF16String exact = {0x00e9,0x6f22,0xd83d,0xde00};
    ob_set_a(r, L"text", &exact);
    for (const char *fixture : {"ASCII", "\xc3\xa9\xe6\xbc\xa2", "\xf0\x9f\x98\x80", ""}) {
        ob_set_s(r, L"fixture", fixture);
        assert(ob_get_a(r, L"fixture", &output));
        assert(output == CUTF8String(reinterpret_cast<const uint8_t *>(fixture)));
    }
    const size_t count = strings.size();
    for (int repeat = 0; repeat < 3; ++repeat) {
        assert(ob_get_a(r, L"text", &output));
        assert(output == CUTF8String(reinterpret_cast<const uint8_t *>("\xc3\xa9\xe6\xbc\xa2\xf0\x9f\x98\x80")));
        assert(!ob_get_b(r, L"text"));
        assert(ob_get_n(r, L"text") == 0);
        assert(!ob_get_c(r, L"text"));
        assert(!ob_get_o(r, L"text"));
        if (strings.size() != count) {
            std::cerr << "getters leaked " << strings.size() - count << " owning property strings\n";
            abort();
        }
    }
    ob_set_b(r, L"bool", true);
    ob_set_n(r, L"number", 3.5);
    assert(ob_get_b(r, L"bool") && ob_get_n(r, L"number") == 3.5);
    output = CUTF8String(reinterpret_cast<const uint8_t *>("unchanged"));
    assert(ob_get_a(r, L"bool", &output));
    assert(output == CUTF8String(reinterpret_cast<const uint8_t *>("unchanged")));
    assert(!ob_get_a(r, L"missing", &output));
    assert(!ob_get_o(r, L"missing") && !ob_get_c(r, L"missing"));
    for (bool collection : {false, true}) {
        auto *nested = reference();
        if (collection) ob_set_c(r, L"nested", nested);
        else ob_set_o(r, L"nested", nested);
        assert(nested->owners == 1);
        assert(!ob_get_b(r, L"nested") && ob_get_n(r, L"nested") == 0);
        assert(ob_get_a(r, L"nested", &output));
        assert(nested->owners == 1);
        if (collection) assert(!ob_get_o(r, L"nested"));
        else assert(!ob_get_c(r, L"nested"));
        assert(nested->owners == 1);
        void *handed = collection ? ob_get_c(r, L"nested") : ob_get_o(r, L"nested");
        assert(handed == nested && nested->owners == 2);
        ob_set_0(r, L"nested");
        assert(nested->owners == 1);
        if (collection) PA_DisposeCollection(handed);
        else PA_DisposeObject(handed);
    }
    for (bool mismatch : {false, true}) {
        stringifyMismatch = mismatch;
        output.clear();
        ob_stringify(r, &output);
        if (!mismatch) assert(output == CUTF8String(reinterpret_cast<const uint8_t *>("{}\xc3\xa9\xf0\x9f\x98\x80")));
        else assert(output.empty());
        assert(references.size() == 1);
    }
    release(r);
}

static void arraysAndContent() {
    const CUTF16String exact = {0x00e9,0x6f22,0xd83d,0xde00};
    PA_Variable array = PA_CreateVariable(eVK_ArrayUnicode);
    PA_ResizeArray(&array, 2);
    auto s = PA_CreateUnistring(const_cast<PA_Unichar *>(exact.c_str()));
    PA_SetStringInArray(array, 0, &s);
    auto borrowed = PA_GetStringInArray(array, 0);
    assert(borrowed.fString == s.fString && text(borrowed) == exact);
    auto independent = PA_CreateUnistring(borrowed.fString);
    PA_SetStringInArray(array, 1, &independent);
    auto replacement = PA_CreateUnistring(const_cast<PA_Unichar *>(exact.c_str()));
    PA_SetStringInArray(array, 0, &replacement);
    assert(text(PA_GetStringInArray(array, 1)) == exact);
    for (int index : {-1, 3}) {
        auto rejected = PA_CreateUnistring(const_cast<PA_Unichar *>(exact.c_str()));
        PA_SetStringInArray(array, index, &rejected);
        assert(text(rejected) == exact);
        PA_DisposeUnistring(&rejected);
    }
    PA_Variable invalid = PA_CreateVariable(eVK_Undefined);
    auto rejected = PA_CreateUnistring(const_cast<PA_Unichar *>(exact.c_str()));
    PA_SetStringInArray(invalid, 0, &rejected);
    invalid.fType = eVK_ArrayUnicode;
    invalid.uValue.fArray.fData = nullptr;
    PA_SetStringInArray(invalid, 0, &rejected);
    PA_DisposeUnistring(&rejected);
    PA_ResizeArray(&array, 0);
    PA_ClearVariable(&array);
    ARRAY_TEXT wrapper;
    wrapper.appendUTF16String(exact.c_str());
    wrapper.appendUTF16String(exact.c_str());
    array = PA_CreateVariable(eVK_Undefined);
    BytePtr params[] = {reinterpret_cast<BytePtr>(&array)};
    wrapper.toParamAtIndex(params, 1);
    ARRAY_TEXT borrowedCopy;
    borrowedCopy.fromParamAtIndex(params, 1);
    PA_ClearVariable(&array);
    CUTF16String copied;
    borrowedCopy.copyUTF16StringAtIndex(&copied, 1);
    assert(copied == exact);
    C_TEXT scalar;
    scalar.setUTF16String(exact.c_str(), static_cast<uint32_t>(exact.size()));
    auto dest = PA_CreateUnistring(const_cast<PA_Unichar *>(exact.c_str()));
    BytePtr scalarParams[] = {reinterpret_cast<BytePtr>(&dest)};
    scalar.toParamAtIndex(scalarParams, 1);
    assert(text(dest) == exact);
    PA_DisposeUnistring(&dest);
    // Record a dormant SDK failure-path limitation without performing an unsafe write.
    array = PA_CreateVariable(eVK_ArrayUnicode);
    PA_Handle h = array.uValue.fArray.fData;
    const size_t capacity = handles.at(h);
    failResize = true;
    PA_ResizeArray(&array, 2);
    failResize = false;
    assert(array.uValue.fArray.fNbElements == 2 && handles.at(h) == capacity);
    array.uValue.fArray.fNbElements = 0;
    PA_ClearVariable(&array);
}

int main(int argc, char **argv) {
    assert(argc == 3);
    allocatedEmpty = std::string(argv[2]) == "allocated";
    gCall4D = engine;
    const std::string mode(argv[1]);
    if (mode.size() == 8 && mode.substr(0, 7) == "setters") {
        assert(mode[7] >= '0' && mode[7] <= '3');
        setters(mode[7] - '0');
    }
    else if (mode == "getters") getters();
    else { assert(mode == "contracts"); arraysAndContent(); }
    assert(strings.empty() && handles.empty() && references.empty());
    std::cout << mode << " " << argv[2] << ": zero SDK strings, handles, and references\n";
}

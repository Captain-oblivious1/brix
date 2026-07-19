import { sharedValue } from "../../shared/src/index";
import { pretty } from "pretty";

console.log(pretty("app using " + sharedValue()));

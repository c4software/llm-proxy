// @ts-check
// La lecture en cours, une seule à la fois quel que soit le moteur : son
// identifiant (« msg:<id> », « tool:<ref> », « try:… »), ou «» ;
// `loading` pendant la synthèse du premier passage (moteur du proxy) ;
// `error` : celle de la dernière lecture `errorId`.
export const state = Vue.reactive({ id: "", loading: false, error: "", errorId: "" });
